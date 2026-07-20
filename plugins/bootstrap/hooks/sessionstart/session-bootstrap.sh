#!/usr/bin/env bash
set -uo pipefail

# session-bootstrap.sh — Thin bash wrapper for the Python bootstrap engine.
#
# Resolves paths, guards for python3, then delegates to the engine.
# Engine's stdout becomes the hook response (JSON with systemMessage).
#
# NOTE: We intentionally do NOT use set -e. With -e, any unexpected command
# failure causes silent exit with no JSON output, and Claude Code shows nothing.
# Instead, we handle errors explicitly and ensure JSON is always emitted.

# Safety net: if the script exits without producing output, emit minimal JSON
HOOK_OUTPUT_EMITTED=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# --- Parse flags ---
FLAG_VERBOSE=""
FLAG_CONSOLE=""
ENGINE_FLAGS=()
for arg in "$@"; do
    case "$arg" in
        --verbose) FLAG_VERBOSE=1; ENGINE_FLAGS+=(--verbose) ;;
        --console) FLAG_CONSOLE=1; ENGINE_FLAGS+=(--console) ;;
        # Interactive fix-all re-run (user consent for elevation): the engine
        # may launch the elevation script itself. Only ever present on an
        # explicit invocation -- hooks.json wires SessionStart with no
        # arguments, so a background pass can never carry it.
        --fix-all) ENGINE_FLAGS+=(--fix-all) ;;
    esac
done

# Derive marketplace name from plugin root path.
# Works for both dev layout (~/Dev/<marketplace>/plugins/bootstrap/)
# and cache layout (~/.claude/plugins/cache/<marketplace>/bootstrap/<version>/).
MARKETPLACE_NAME="$(basename "$(cd "$PLUGIN_ROOT/../.." && pwd)")"
BOOTSTRAP_LABEL="${MARKETPLACE_NAME}:bootstrap"
PLUGIN_DATA="${HOME}/.claude/plugins/data/${MARKETPLACE_NAME}/bootstrap"

# Set trap after BOOTSTRAP_LABEL is defined so variable expands correctly
# In console mode, no JSON safety net needed — plain text output.
# In normal mode, JSON is emitted immediately below, so the trap only needs
# to write failure info to the display file for the UserPromptSubmit
# display hook to surface.
if [ -z "$FLAG_CONSOLE" ]; then
    trap '[ -z "$HOOK_OUTPUT_EMITTED" ] && mkdir -p "$PLUGIN_DATA" && printf "{\"continue\": true, \"suppressOutput\": false, \"systemMessage\": \"%s: shell error\"}" "'"${BOOTSTRAP_LABEL}"'" > "$PLUGIN_DATA/bootstrap_display.pending"' EXIT
fi

# --- Capture hook input from stdin and record start time ---
# In console mode, skip stdin read (no hook JSON piped in)
if [ -n "$FLAG_CONSOLE" ]; then
    HOOK_INPUT=""
else
    HOOK_INPUT=$(cat)
fi
HOOK_START_EPOCH=$(date +%s 2>/dev/null || echo "0")

# --- Session guard: prevent double invocation in same session ---
# Two-layer guard: session_id (if available) AND timestamp-based cooldown.
# Non-blocking hooks may not receive stdin, so session_id alone is insufficient.
if [ -n "$HOOK_INPUT" ]; then
    _GUARD_SID=$(echo "$HOOK_INPUT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | grep -o '"[^"]*"$' | tr -d '"' || true)
fi
_GUARD_FILE="$PLUGIN_DATA/last_session_id"
# Per-project cooldown: key the cooldown file by sha1(project_dir) so launching
# claude in project B shortly after project A doesn't silently skip B's
# bootstrap. PWD is Claude Code's launch CWD (matches what session-bootstrap
# passes to the engine as --project-dir). Falls back to a "_global_" bucket
# when no usable hash tool is available.
_COOLDOWN_DIR="$PLUGIN_DATA/cooldowns"
_PROJECT_KEY=""
if command -v sha1sum >/dev/null 2>&1; then
    _PROJECT_KEY=$(printf '%s' "$PWD" | sha1sum | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
    _PROJECT_KEY=$(printf '%s' "$PWD" | shasum -a 1 | awk '{print $1}')
fi
if [ -z "$_PROJECT_KEY" ]; then
    _PROJECT_KEY="_global_"
fi
_COOLDOWN_FILE="$_COOLDOWN_DIR/last_run_epoch.$_PROJECT_KEY"
_COOLDOWN_SECS=3600  # 60-minute per-project cooldown; reset via bootstrap-reset-cooldown
mkdir -p "$PLUGIN_DATA" "$_COOLDOWN_DIR"

# --- Per-session marker (SessionStart-missed rescue's detection signal) ---
# Touch sessions/<session_id> at ENTRY -- before the gates, so even a gate-
# skipped invocation records "a pass was invoked for this session". The
# UserPromptSubmit rescue in bootstrap-display.sh keys on this marker: missing
# for the prompt's session_id means SessionStart never fired this session
# (fresh machine mid-sync) and it launches this script itself. Entry-time (not
# completion-time) so a fast first prompt (claude -p) sees it within
# milliseconds and stands down instead of double-launching. Old markers are
# pruned after the gates below. Not the Layer-1 guard stamp: that's a single
# global slot that a concurrent session overwrites, and bootstrap-reset-cooldown
# deletes it (a reset must re-arm the NEXT SessionStart, not a mid-session run).
if [ -n "${_GUARD_SID:-}" ]; then
    _SID_SAFE=$(printf '%s' "$_GUARD_SID" | tr -cd 'A-Za-z0-9._-')
    if [ -n "$_SID_SAFE" ]; then
        mkdir -p "$PLUGIN_DATA/sessions" && : > "$PLUGIN_DATA/sessions/$_SID_SAFE" 2>/dev/null || true
    fi
fi

# Registry files Claude Code rewrites whenever a plugin is installed/updated/
# rescoped (installed_plugins.json) or a marketplace is added/refreshed
# (known_marketplaces.json). PLUGIN_DATA is
# ~/.claude/plugins/data/<marketplace>/bootstrap, so the plugins root is three
# dirs up. Used by the cooldown gate below to detect "a plugin changed since we
# last ran" and bypass the throttle so the new version is provisioned promptly.
_PLUGINS_ROOT="$(cd "$PLUGIN_DATA/../../.." 2>/dev/null && pwd)"
_INSTALLED_PLUGINS="$_PLUGINS_ROOT/installed_plugins.json"
_KNOWN_MARKETPLACES="$_PLUGINS_ROOT/known_marketplaces.json"

# --- Persistent alert (bootstrap_alert.json) ---
# The bootstrap engine writes bootstrap_alert.json when there's an unresolved
# user-actionable failure (e.g. a python stub on the system PATH). The alert
# file lives until the engine confirms the fix on a later run. Its presence
# bypasses BOTH skip gates below, so a full pass re-checks every session and
# re-emits fresh results to bootstrap_display.pending itself — that is the
# "show the alert each session until resolved" behavior. Deliberately NOT
# copied to the pending file here: an entry-time copy raced the pass it
# triggers (an early prompt consumed the stale copy, a later prompt consumed
# the pass's fresh emission — the same failure displayed twice per session,
# sometimes after it was already fixed).
_ALERT_FILE="$PLUGIN_DATA/bootstrap_alert.json"

# Layer 1: session_id guard (works when stdin is available). Skip a repeat of
# the SAME session_id — UNLESS a plugin registry file is newer than the guard
# stamp (a plugin was installed/updated/rescoped, or a marketplace refreshed,
# since we last ran) or there's an unresolved alert. Without this bypass a
# resumed session (`claude --resume` re-presents the ORIGINAL session_id) skips
# the pass even right after an update landed, so the new version never gets
# provisioned until some unrelated fresh session — the two-restart trap. Mirrors
# Layer 2's bypass; the printf below re-stamps $_GUARD_FILE (bumping its mtime)
# so the next genuine same-session repeat with no new change skips again. `-nt`
# is false when the registry file is missing, so the guard is honored by default.
if [ -n "${_GUARD_SID:-}" ]; then
    if [ -f "$_GUARD_FILE" ] && [ "$(cat "$_GUARD_FILE" 2>/dev/null)" = "$_GUARD_SID" ] \
       && [ ! -f "$_ALERT_FILE" ] \
       && [ ! "$_INSTALLED_PLUGINS" -nt "$_GUARD_FILE" ] \
       && [ ! "$_KNOWN_MARKETPLACES" -nt "$_GUARD_FILE" ]; then
        HOOK_OUTPUT_EMITTED=1
        exit 0
    fi
    printf '%s' "$_GUARD_SID" > "$_GUARD_FILE"
fi

# Layer 2: timestamp cooldown, keyed per-project (works regardless of stdin).
# Bypass the cooldown when there's an unresolved persistent alert — we want
# the engine to re-check on every session in that case so it can confirm the
# fix and clear the alert promptly. Steady-state success still gets the
# per-project throttle.
#
# Also bypass when a plugin registry file is newer than the cooldown stamp: that
# means Claude Code installed/updated/rescoped a plugin (installed_plugins.json)
# or added/refreshed a marketplace (known_marketplaces.json) since bootstrap last
# ran, so the new version's deps/shared-libs need provisioning NOW rather than
# after the throttle expires. A cooldown SKIP never refreshes $_COOLDOWN_FILE
# (the stamp is written only when a pass actually runs, below), so this bypass
# stays armed across every restart until a real pass resyncs — fixing the
# "version bumped but bootstrap throttle-skipped the resync" stale-shared-lib
# bug. `-nt` is false when the registry file is missing, so the cooldown is
# honored by default. See CLAUDE.md's "Per-project cooldown" / cooldown insights.
if [ -f "$_COOLDOWN_FILE" ] && [ ! -f "$_ALERT_FILE" ] \
   && [ ! "$_INSTALLED_PLUGINS" -nt "$_COOLDOWN_FILE" ] \
   && [ ! "$_KNOWN_MARKETPLACES" -nt "$_COOLDOWN_FILE" ]; then
    _LAST_RUN=$(cat "$_COOLDOWN_FILE" 2>/dev/null || echo "0")
    _NOW=$(date +%s 2>/dev/null || echo "0")
    _AGE=$((_NOW - _LAST_RUN))
    if [ $_AGE -lt $_COOLDOWN_SECS ]; then
        # Cooldown skip is silent: it isn't a remediation, it's a throttle. The
        # cooldown file's mtime ($_COOLDOWN_FILE) records when bootstrap last
        # ran, which is enough to distinguish "throttled" from "passed clean"
        # during debugging. Use 'bootstrap-reset-cooldown' to force a re-run.
        HOOK_OUTPUT_EMITTED=1
        exit 0
    fi
fi
printf '%s' "$HOOK_START_EPOCH" > "$_COOLDOWN_FILE"

# Prune stale per-session markers (and rescue locks) on real passes only --
# session ids are never reused, so week-old markers are dead weight.
find "$PLUGIN_DATA/sessions" -type f -mtime +7 -delete 2>/dev/null || true

# --- Emit hook JSON immediately (fire-and-forget) ---
# In normal mode, emit JSON now so Claude Code doesn't block waiting for engine output.
# The engine writes its display JSON to bootstrap_display.pending for the
# UserPromptSubmit display hook.
if [ -z "$FLAG_CONSOLE" ]; then
    echo '{"continue": true, "suppressOutput": true}'
    HOOK_OUTPUT_EMITTED=1
fi

# --- Logging ---
# Collect entries in memory; write as a block at the end (with header) only if non-empty.
SHELL_LOG_ENTRIES=()

log_entry() {
    local msg="$1"
    SHELL_LOG_ENTRIES+=("$msg")
    # In console mode, also print to stdout immediately
    if [ -n "$FLAG_CONSOLE" ]; then
        echo "$msg"
    fi
}

flush_log() {
    # Write collected entries as a block with a "Shell" header, only if non-empty.
    if [ ${#SHELL_LOG_ENTRIES[@]} -eq 0 ]; then
        return
    fi
    local ts
    ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "unknown-time")"
    mkdir -p "$PLUGIN_DATA"
    {
        echo "--- Shell $ts ---"
        for entry in "${SHELL_LOG_ENTRIES[@]}"; do
            echo "$entry"
        done
    } >> "$PLUGIN_DATA/bootstrap.log"
}

# --- Read log_success_shell from config (pre-Python, so use grep) ---
LOG_SUCCESS_SHELL="false"
CONFIG_FILE="$PLUGIN_DATA/config.json"
if [ -f "$CONFIG_FILE" ]; then
    # Extract value: grep for the key, strip to true/false
    val=$(grep -o '"log_success_shell"[[:space:]]*:[[:space:]]*[a-z]*' "$CONFIG_FILE" 2>/dev/null | grep -o '[a-z]*$' || echo "false")
    if [ "$val" = "true" ]; then
        LOG_SUCCESS_SHELL="true"
    fi
fi
# --verbose and --console override config to show all shell entries
if [ -n "$FLAG_VERBOSE" ] || [ -n "$FLAG_CONSOLE" ]; then
    LOG_SUCCESS_SHELL="true"
fi

# --- Provisioning: everything below runs DETACHED in normal mode ---
# MEASURED (2026-07-20, synthetic SessionStart hooks + timed `claude -p`):
# Claude Code blocks session readiness on the SessionStart hook's COMPLETION,
# where completion = process exit AND stdout-pipe EOF -- a background child
# that merely inherits the hook's stdout holds the session exactly as long as
# foreground work would, while a child with stdin/stdout/stderr redirected
# costs zero. So the foreground path above stays at "read stdin + gates + emit
# JSON" (tens of ms), and every remaining step -- PATH setup, reset-script
# install, Python ensure (including the cold-start standalone download), the
# Windows registry PATH writes (two PowerShell invocations), and the engine
# launch -- lives in _provision, spawned detached with all fds redirected.
# User-facing output is unaffected: it always flowed through
# bootstrap_display.pending -> the UserPromptSubmit display hook, never
# through SessionStart stdout. Console mode runs _provision synchronously in
# the main shell (its engine `exec` at the end replaces the process, exactly
# as before).
_provision() {

# --- Ensure required dirs are at front of PATH ---
# ~/.local/bin: tools installed by bootstrap (uv, git wrappers, etc.)
# ~/.local/share/python-standalone/python: standalone Python + DLLs (Windows needs this for DLL resolution)
LOCAL_BIN="${HOME}/.local/bin"
STANDALONE_PYTHON_BIN="${HOME}/.local/share/python-standalone/python"
case ":${PATH}:" in
    *":${LOCAL_BIN}:"*) ;;
    *) export PATH="${LOCAL_BIN}:${PATH}" ;;
esac
case ":${PATH}:" in
    *":${STANDALONE_PYTHON_BIN}:"*) ;;
    *) export PATH="${STANDALONE_PYTHON_BIN}:${PATH}" ;;
esac

# Detect OS once — used both by the reset-script install below and by the
# Python install/install-path logic further down.
OS="$(uname -s)"

# --- Install bootstrap-reset-cooldown into ~/.local/bin ---
# A user-facing tool to clear the per-project cooldown when bootstrap needs to
# re-run sooner than the throttle allows. Re-installed every session so it
# stays in sync with the cached plugin version.
_RESET_SRC="$PLUGIN_ROOT/scripts/bootstrap-reset-cooldown.sh"
_RESET_DST="$LOCAL_BIN/bootstrap-reset-cooldown"
if [ -f "$_RESET_SRC" ]; then
    mkdir -p "$LOCAL_BIN"
    if [[ "$OS" == MINGW* ]] || [[ "$OS" == MSYS* ]]; then
        # Windows: symlinks need elevation; just copy.
        if ! cmp -s "$_RESET_SRC" "$_RESET_DST" 2>/dev/null; then
            cp -f "$_RESET_SRC" "$_RESET_DST" 2>/dev/null && chmod +x "$_RESET_DST" 2>/dev/null
        fi
    else
        # Unix: symlink so updates flow automatically when the plugin cache refreshes.
        if [ ! -L "$_RESET_DST" ] || [ "$(readlink "$_RESET_DST" 2>/dev/null)" != "$_RESET_SRC" ]; then
            ln -sf "$_RESET_SRC" "$_RESET_DST"
        fi
    fi
fi

# --- Ensure Python is installed in ~/.local/bin ---
# We always use our standalone Python in ~/.local/bin. System Python is not used.
# Check if it's in place and works; install standalone if not.

PYTHON=""
STANDALONE_DIR="${HOME}/.local/share/python-standalone"

# Schannel (Windows-native TLS) can fail with CRYPT_E_NO_REVOCATION_CHECK on
# corporate networks. --ssl-revoke-best-effort still checks when possible but
# doesn't hard-fail when the revocation server is unreachable.
CURL_FLAGS=()
if [[ "$OS" == MINGW* ]] || [[ "$OS" == MSYS* ]]; then
    CURL_FLAGS=(--ssl-revoke-best-effort)
fi

if [[ "$OS" == MINGW* ]] || [[ "$OS" == MSYS* ]]; then
    # Windows: use standalone executable directly — no hard link needed since the
    # standalone dir is on PATH (DLLs are co-located with the executable there).
    WANT_PYTHON="${STANDALONE_DIR}/python/python.exe"
    STANDALONE_PYTHON="${STANDALONE_DIR}/python/python.exe"
else
    WANT_PYTHON="${LOCAL_BIN}/python3"
    STANDALONE_PYTHON="${STANDALONE_DIR}/python/bin/python3"
fi

# Check 1: our python is in place and works
if [ -x "$WANT_PYTHON" ] && "$WANT_PYTHON" -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" 2>/dev/null; then
    PYTHON="$WANT_PYTHON"
    if [ "$LOG_SUCCESS_SHELL" = "true" ]; then
        log_entry "python3: ok - found at $WANT_PYTHON"
    fi
# Check 2 (Unix only): standalone installed but ~/.local/bin symlink missing — restore it
elif [[ "$OS" != MINGW* ]] && [[ "$OS" != MSYS* ]] && [ -x "$STANDALONE_PYTHON" ] && "$STANDALONE_PYTHON" -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" 2>/dev/null; then
    mkdir -p "$LOCAL_BIN"
    ln -sf "$STANDALONE_PYTHON" "$WANT_PYTHON"
    log_entry "python3: restored symlink $WANT_PYTHON -> $STANDALONE_PYTHON"
    PYTHON="$WANT_PYTHON"
fi

# Check 3: nothing works — download and install standalone
if [ -z "$PYTHON" ]; then
    log_entry "python3: not installed, downloading standalone"

    PY_VERSION="3.12.9"
    RELEASE_TAG="20250317"
    ARCH="$(uname -m)"

    if [[ "$OS" == "Darwin" ]]; then
        [[ "$ARCH" == "arm64" ]] && TRIPLE="aarch64-apple-darwin" || TRIPLE="x86_64-apple-darwin"
    elif [[ "$OS" == "Linux" ]]; then
        [[ "$ARCH" == "aarch64" ]] && TRIPLE="aarch64-unknown-linux-gnu" || TRIPLE="x86_64-unknown-linux-gnu"
    elif [[ "$OS" == MINGW* ]] || [[ "$OS" == MSYS* ]]; then
        TRIPLE="x86_64-pc-windows-msvc"
    else
        log_entry "python3: FAILED - unsupported platform for auto-install ($OS)"
        flush_log
        mkdir -p "$PLUGIN_DATA"
        printf '{"continue": true, "suppressOutput": false, "systemMessage": "%s -> Python 3 is not installed and this platform has no unattended installer. Claude will ask if you want help installing it.", "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "%s -> ASK THE USER before doing anything: Python 3 is missing and this platform (%s) has no unattended installer, so bootstrap cannot run until Python 3.x is on PATH. Installing Python needs the user, so use the AskUserQuestion tool (do not merely mention it). Ask whether to install Python 3 now, with exactly two options in order: Do nothing (the default -- bootstrap re-checks next session), and Install Python (help them install Python 3.x for their OS and get it on PATH). Act only if they pick Install Python."}}\n' "${BOOTSTRAP_LABEL}" "${BOOTSTRAP_LABEL}" "${OS}" > "$PLUGIN_DATA/bootstrap_display.pending"
        exit 0
    fi

    ARCHIVE="cpython-${PY_VERSION}+${RELEASE_TAG}-${TRIPLE}-install_only_stripped.tar.gz"
    URL="https://github.com/indygreg/python-build-standalone/releases/download/${RELEASE_TAG}/${ARCHIVE}"

    log_entry "python3: downloading $ARCHIVE"
    mkdir -p "$STANDALONE_DIR"
    _dl_tmp="$STANDALONE_DIR/$ARCHIVE"
    if ! curl -LsSf "${CURL_FLAGS[@]}" "$URL" -o "$_dl_tmp" 2>/dev/null || ! tar xzf "$_dl_tmp" -C "$STANDALONE_DIR" 2>/dev/null; then
        rm -f "$_dl_tmp" 2>/dev/null
        log_entry "python3: FAILED - download error"
        flush_log
        mkdir -p "$PLUGIN_DATA"
        printf '{"continue": true, "suppressOutput": false, "systemMessage": "%s -> Automatic Python 3 install failed (download error). Claude will ask if you want help installing it.", "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "%s -> ASK THE USER: bootstrap tried to auto-install Python 3 but the download failed, so bootstrap cannot run until Python 3.x is on PATH. Installing Python needs the user, so use the AskUserQuestion tool (do not merely mention it). Ask whether to install Python 3 now, with exactly two options in order: Do nothing (the default -- bootstrap retries next session), and Install Python (help them install Python 3.x for their OS -- retry the download or use the OS package manager -- and get it on PATH). Act only if they pick Install Python."}}\n' "${BOOTSTRAP_LABEL}" "${BOOTSTRAP_LABEL}" > "$PLUGIN_DATA/bootstrap_display.pending"
        exit 0
    fi
    rm -f "$_dl_tmp"

    if [[ "$OS" == MINGW* ]] || [[ "$OS" == MSYS* ]]; then
        # Windows: standalone exe is on PATH via STANDALONE_PYTHON_BIN — no symlink needed
        log_entry "python3: installed standalone at $STANDALONE_PYTHON"
        PYTHON="$STANDALONE_PYTHON"
    else
        mkdir -p "$LOCAL_BIN"
        ln -sf "$STANDALONE_PYTHON" "$WANT_PYTHON"
        log_entry "python3: installed standalone, linked to $WANT_PYTHON"
        PYTHON="$WANT_PYTHON"
    fi
fi

# --- Persist PATH entries to Windows User PATH (registry) ---
# On Windows, write ~/.local/bin and the standalone Python dir to the registry
# so that future Claude Code sessions inherit them regardless of parent shell.
if [[ "$OS" == MINGW* ]] || [[ "$OS" == MSYS* ]]; then
    # Anchor powershell.exe to an absolute path. SessionStart hooks can
    # inherit a stripped PATH (no System32\WindowsPowerShell\v1.0) and a
    # bare-name lookup then fails with "not found" — silently here, because
    # of `|| true` below. Resolving SYSTEMROOT once up front avoids that.
    _SYSROOT_U=$(cygpath -u "${SYSTEMROOT:-C:\\Windows}" 2>/dev/null || echo "/c/Windows")
    PWSH_EXE="$_SYSROOT_U/System32/WindowsPowerShell/v1.0/powershell.exe"
    [ -x "$PWSH_EXE" ] || PWSH_EXE="powershell.exe"
    for _path_entry in "$LOCAL_BIN" "$STANDALONE_PYTHON_BIN"; do
        _win_path=$(cygpath -w "$_path_entry" 2>/dev/null || echo "$_path_entry" | sed 's|/|\\|g')
        _ps_result=$("$PWSH_EXE" -NoProfile -NonInteractive -Command "
            \$entry = '$_win_path'
            \$current = [Environment]::GetEnvironmentVariable('Path', 'User')
            if (-not \$current) { \$current = '' }
            \$parts = \$current -split ';' | Where-Object { \$_ -ne '' }
            \$norm = \$entry.TrimEnd('\\')
            \$found = \$false
            foreach (\$p in \$parts) { if (\$p.TrimEnd('\\') -ieq \$norm) { \$found = \$true; break } }
            if (-not \$found) {
                \$newPath = (\$entry + ';' + \$current).TrimEnd(';')
                [Environment]::SetEnvironmentVariable('Path', \$newPath, 'User')
                Write-Output 'added'
            } else {
                Write-Output 'already_present'
            }
        " 2>/dev/null) || true
        if [ "$_ps_result" = "added" ]; then
            log_entry "PATH: added $_win_path to Windows User PATH (registry)"
        elif [ "$LOG_SUCCESS_SHELL" = "true" ] && [ "$_ps_result" = "already_present" ]; then
            log_entry "PATH: $_win_path already in Windows User PATH"
        fi
    done
fi

# --- Extract hook input fields for logging ---
HOOK_SOURCE=""
HOOK_SESSION_ID=""
HOOK_MODEL=""
if command -v jq &>/dev/null && [ -n "$HOOK_INPUT" ]; then
    HOOK_SOURCE=$(echo "$HOOK_INPUT" | jq -r '.source // empty' 2>/dev/null || true)
    HOOK_SESSION_ID=$(echo "$HOOK_INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
    HOOK_MODEL=$(echo "$HOOK_INPUT" | jq -r '.model // empty' 2>/dev/null || true)
elif [ -n "$HOOK_INPUT" ]; then
    # Fallback: grep for fields (no jq available)
    HOOK_SOURCE=$(echo "$HOOK_INPUT" | grep -o '"source"[[:space:]]*:[[:space:]]*"[^"]*"' | grep -o '"[^"]*"$' | tr -d '"' || true)
    HOOK_SESSION_ID=$(echo "$HOOK_INPUT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | grep -o '"[^"]*"$' | tr -d '"' || true)
    HOOK_MODEL=$(echo "$HOOK_INPUT" | grep -o '"model"[[:space:]]*:[[:space:]]*"[^"]*"' | grep -o '"[^"]*"$' | tr -d '"' || true)
fi
if [ "$LOG_SUCCESS_SHELL" = "true" ]; then
    log_entry "hook: source=$HOOK_SOURCE session=$HOOK_SESSION_ID model=$HOOK_MODEL"
fi

# --- Flush shell log entries (if any) before handing off to engine ---
# In console mode, skip file writes (entries were already printed to stdout)
if [ -z "$FLAG_CONSOLE" ]; then
    flush_log
fi

# --- Invoke Engine ---

if [ -z "$FLAG_CONSOLE" ]; then
    # Background mode: engine writes display JSON to file. Stdout+stderr are
    # captured to a debug log so silent engine failures (which would otherwise
    # vanish into /dev/null) can be inspected after the fact.
    "$PYTHON" "${PLUGIN_ROOT}/engine/bootstrap_engine.py" \
        --plugin-root "$PLUGIN_ROOT" \
        --data-dir "$PLUGIN_DATA" \
        --hook-start-epoch "$HOOK_START_EPOCH" \
        --project-dir "$PWD" \
        --background \
        "${ENGINE_FLAGS[@]}" > "$PLUGIN_DATA/engine_output.log" 2>&1 &
else
    # Console mode: synchronous, plain text to stdout
    exec "$PYTHON" "${PLUGIN_ROOT}/engine/bootstrap_engine.py" \
        --plugin-root "$PLUGIN_ROOT" \
        --data-dir "$PLUGIN_DATA" \
        --hook-start-epoch "$HOOK_START_EPOCH" \
        --project-dir "$PWD" \
        "${ENGINE_FLAGS[@]}"
fi

}

# --- Dispatch _provision (see the MEASURED note at its definition) ---
# Normal mode: detached, ALL fds redirected -- </dev/null so the child never
# holds the hook's stdin, >/dev/null 2>&1 so it never holds the stdout pipe
# Claude Code waits on (the engine inside re-redirects its own output to
# engine_output.log; user-facing content flows via bootstrap_display.pending).
# The backgrounded subshell does not inherit the EXIT trap (bash resets traps
# in subshells), so the trap can't double-fire; the failure paths inside
# _provision write bootstrap_display.pending explicitly, which is the channel
# that actually reaches the user. Console mode: synchronous, same shell, so
# output prints and the engine exec behaves exactly as it always has.
if [ -n "$FLAG_CONSOLE" ]; then
    _provision
else
    _provision </dev/null >/dev/null 2>&1 &
fi
