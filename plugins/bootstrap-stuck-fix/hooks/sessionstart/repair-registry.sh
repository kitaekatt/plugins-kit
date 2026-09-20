#!/usr/bin/env bash
# SessionStart hook for bootstrap-stuck-fix.
#
# Repairs the two independent defects that permanently wedge an affected machine
# on an old bootstrap version (full writeups in each script):
#
#   repair_registry.py     - malformed duplicate registry record
#   repair_update_scope.py - update requested at the manifest's scope rather
#                            than the scope the plugin is installed at
#
# Both run: they are separate defects and a machine can have either or both.
# Registry repair goes first -- it can leave a single well-formed record behind,
# which is exactly the shape the scope repair then acts on.
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

SCRIPTS="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}/scripts"
[[ -d "$SCRIPTS" ]] || exit 0

# Find a usable Python. The Microsoft Store stub on Windows is on PATH as
# `python`/`python3` but is not a real interpreter -- it prints a "not found"
# notice and exits non-zero, so the -c probe rejects it. The bootstrap-provisioned
# standalone build is preferred because affected machines are, by definition,
# ones where bootstrap has already run.
PY=""
for candidate in \
    "$HOME/.local/share/python-standalone/python/python.exe" \
    "$HOME/.local/share/python-standalone/python/bin/python3"
do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

# BOOTSTRAP_PYTHON is accepted only as a fallback for the deterministic
# candidates above, and only when it resolves inside the standalone install
# directory -- never trusted blind, which would reintroduce a dependency this
# plugin exists to not have.
if [[ -z "$PY" ]] && [[ -n "${BOOTSTRAP_PYTHON:-}" ]] && [[ -x "$BOOTSTRAP_PYTHON" ]]; then
    case "$(cd "$(dirname "$BOOTSTRAP_PYTHON")" 2>/dev/null && pwd -P)" in
        "$(cd "${HOME}/.local/share/python-standalone" 2>/dev/null && pwd -P)"/*)
            "$BOOTSTRAP_PYTHON" -c "" >/dev/null 2>&1 && PY="$BOOTSTRAP_PYTHON"
            ;;
    esac
fi

if [[ -z "$PY" ]]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "" >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    done
fi
[[ -n "$PY" ]] || exit 0

for script in repair_registry.py repair_update_scope.py; do
    [[ -f "$SCRIPTS/$script" ]] || continue
    "$PY" "$SCRIPTS/$script" 2>/dev/null || true
done
exit 0
