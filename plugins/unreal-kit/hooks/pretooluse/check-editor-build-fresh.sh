#!/usr/bin/env bash
# check-editor-build-fresh.sh -- PreToolUse hook for mcp__unreal-engine__*
#
# Two-stage architecture for low foreground latency:
#   - Foreground: substring check on tool_name, file-existence check on a
#     marker, emit warning if marker is present, spawn detector in
#     background, exit. Target: <30ms.
#   - Background: detect-editor-stale.py compares
#     UnrealEditor-BuildSettings.dll mtime vs Engine/Build/Build.version
#     mtime and writes/removes the marker. Latency does not matter -- the
#     foreground exits before the detector finishes.
#
# Marker semantics: file exists -> stale -> emit warning. The first MCP call
# after install runs against an absent marker (no warning emitted) but kicks
# off detection; subsequent calls reflect the latest detection. Self-corrects
# in one call after rebuild.

set -uo pipefail

INPUT=$(< /dev/stdin)

# Cheap regex check on the raw JSON -- no jq subprocess. Tolerates both
# compact ("tool_name":"...") and pretty-printed ("tool_name": "...").
[[ "$INPUT" =~ \"tool_name\"[[:space:]]*:[[:space:]]*\"mcp__unreal-engine__ ]] || exit 0

# Extract cwd via the same regex shape; unescape JSON \\ -> \ for Windows paths.
CWD=""
if [[ "$INPUT" =~ \"cwd\"[[:space:]]*:[[:space:]]*\"([^\"]+)\" ]]; then
    CWD="${BASH_REMATCH[1]//\\\\/\\}"
fi
[[ -n "$CWD" ]] || exit 0

MARKER="$CWD/.local-data/plugins-kit/unreal-kit/editor-stale.flag"

if [[ -f "$MARKER" ]]; then
    # PreToolUse hook output shape:
    #   permissionDecision="allow" -- explicitly allow the call to proceed.
    #     Required for the harness to process the rest of hookSpecificOutput.
    #   permissionDecisionReason -- shown to the user in the CLI (NOT shown to Claude).
    #   additionalContext -- injected into Claude's context for the call (NOT shown to user).
    cat <<'HOOKEOF'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"Unreal editor build is stale -- rebuild the editor before saving any asset. UnrealEditor-BuildSettings.dll is older than Engine/Build/Build.version, so saves will be stamped with Changelist=0 and rejected by the cooker as 'empty engine version'.","additionalContext":"Editor requires rebuild before assets can be safely saved. UnrealEditor-BuildSettings.dll mtime is older than Engine/Build/Build.version, which means the running editor was launched without rebuilding after the latest sync. Asset saves through MCP will stamp Summary.SavedByEngineVersion with Changelist=0 and be rejected by the cooker as 'empty engine version'. Run a build before any save tool call."}}
HOOKEOF
fi

# Spawn the detector fully detached so the foreground returns immediately.
# The subshell-and-background trick orphans the child to init/system.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DETECTOR="$SCRIPT_DIR/detect-editor-stale.py"

if [[ -f "$DETECTOR" ]]; then
    (
        # Deterministic standalone path first; BOOTSTRAP_PYTHON accepted only
        # as a fallback whose realpath is inside the standalone install
        # directory; then whatever is on PATH. Resolved here (in the
        # background) rather than in the foreground so this never touches the
        # <30ms latency budget above.
        _OS="$(uname -s 2>/dev/null || echo unknown)"
        if [[ "$_OS" == MINGW* ]] || [[ "$_OS" == MSYS* ]] || [[ "$_OS" == CYGWIN* ]]; then
            _DETECT_PY="${HOME}/.local/share/python-standalone/python/python.exe"
        else
            _DETECT_PY="${HOME}/.local/share/python-standalone/python/bin/python3"
        fi
        if [[ ! -x "$_DETECT_PY" ]] && [[ -n "${BOOTSTRAP_PYTHON:-}" ]] && [[ -x "$BOOTSTRAP_PYTHON" ]]; then
            case "$(cd "$(dirname "$BOOTSTRAP_PYTHON")" 2>/dev/null && pwd -P)" in
                "$(cd "${HOME}/.local/share/python-standalone" 2>/dev/null && pwd -P)"/*) _DETECT_PY="$BOOTSTRAP_PYTHON" ;;
            esac
        fi
        [[ -x "$_DETECT_PY" ]] || _DETECT_PY="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
        [[ -n "$_DETECT_PY" ]] || exit 0
        printf '%s' "$INPUT" \
        | "$_DETECT_PY" "$DETECTOR" \
            >/dev/null 2>&1 &
    ) &
fi

exit 0
