#!/usr/bin/env bash
# SessionStart hook for bootstrap-stuck-fix.
#
# Repairs the malformed duplicate registry record that permanently wedges an
# affected machine on an old bootstrap version (see scripts/repair_registry.py
# for the full defect writeup).
#
# DEPENDENCY-FREE BY CONSTRUCTION. This plugin exists precisely because the
# bootstrap engine cannot fix this on an affected machine -- so it must not
# depend on bootstrap, on a provisioned venv, or on any other plugin. Stock
# shell plus whichever Python it can find, nothing else. Same discipline as
# claude-settings' check-claude-symlink.sh.
#
# It NEVER fails a session: every path exits 0. A remediation that can break a
# session is worse than the wedge it repairs.

set -u

SCRIPT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}/scripts/repair_registry.py"
[[ -f "$SCRIPT" ]] || exit 0

# Find a usable Python. The Microsoft Store stub on Windows is on PATH as
# `python`/`python3` but is not a real interpreter -- it prints a "not found"
# notice and exits non-zero, so the -c probe rejects it. The bootstrap-provisioned
# standalone build is preferred because affected machines are, by definition,
# ones where bootstrap has already run.
PY=""
for candidate in \
    "$HOME/.local/share/python-standalone/python/python.exe" \
    "$HOME/.local/share/python-standalone/python/bin/python3" \
    python3 \
    python
do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
[[ -n "$PY" ]] || exit 0

"$PY" "$SCRIPT" 2>/dev/null || true
exit 0
