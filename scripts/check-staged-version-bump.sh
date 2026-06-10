#!/bin/sh
# check-staged-version-bump.sh -- Block commits whose staged changes touch
# plugins/<name>/ without also staging a version change in that plugin's
# .claude-plugin/plugin.json.
#
# Why: the repo's most-documented failure mode is "code changed but version
# didn't" (silent divergence / burned versions / invisible manifest edits --
# see root CLAUDE.md gotcha 3 and the manifest_changes_need_version_bump
# insight). This check turns that from a remembered rule into an enforced one.
#
# Escape hatch (intentional dev commits between publish checkpoints):
#     PLUGINS_KIT_SKIP_BUMP_CHECK=1 git commit ...
# or bypass all hooks with `git commit --no-verify`.
#
# Called by scripts/pre-commit-version-check.sh; POSIX sh, no bashisms.

set -eu

if [ "${PLUGINS_KIT_SKIP_BUMP_CHECK:-0}" = "1" ]; then
    exit 0
fi

cd "$(git rev-parse --show-toplevel)"

staged=$(git diff --cached --name-only)
[ -n "$staged" ] || exit 0

# Plugin names with staged changes anywhere under plugins/<name>/
names=$(printf '%s\n' "$staged" | sed -n 's|^plugins/\([^/][^/]*\)/.*|\1|p' | sort -u)
[ -n "$names" ] || exit 0

missing=""
for name in $names; do
    pj="plugins/$name/.claude-plugin/plugin.json"
    # Plugin dir being deleted entirely (or not a real plugin dir): nothing to bump.
    [ -f "$pj" ] || continue
    if git diff --cached -U0 -- "$pj" | grep -Eq '^[+-].*"version"[[:space:]]*:'; then
        continue
    fi
    missing="$missing $name"
done

[ -z "$missing" ] && exit 0

{
    echo "Staged changes touch plugin(s) with no staged version bump:"
    for name in $missing; do
        echo "  - $name (plugins/$name/.claude-plugin/plugin.json version unchanged)"
    done
    echo ""
    echo "Published consumers only refetch a plugin when its version changes;"
    echo "code or manifest edits without a bump are invisible to them (root"
    echo "CLAUDE.md gotcha 3). Bump the version and run"
    echo "\`python scripts/regen_marketplace.py\`, or -- for an intentional"
    echo "dev-branch commit between publish checkpoints -- bypass with:"
    echo "  PLUGINS_KIT_SKIP_BUMP_CHECK=1 git commit ...   (or git commit --no-verify)"
} >&2
exit 1
