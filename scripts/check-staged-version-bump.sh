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

# Version stated by a staged (index) file. Reads the index, not the worktree,
# because the index is what the commit will actually contain.
staged_pyproject_version() {
    git show ":plugins/$1/pyproject.toml" 2>/dev/null \
        | sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1
}

staged_plugin_json_version() {
    git show ":plugins/$1/.claude-plugin/plugin.json" 2>/dev/null \
        | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1
}

missing=""
for name in $names; do
    pj="plugins/$name/.claude-plugin/plugin.json"
    # Plugin dir being deleted entirely (or not a real plugin dir): nothing to bump.
    [ -f "$pj" ] || continue
    if git diff --cached -U0 -- "$pj" | grep -Eq '^[+-].*"version"[[:space:]]*:'; then
        continue
    fi

    # Pure pyproject version sync: nothing to bump, so requiring a bump here is
    # a false positive -- and a deadlock. check_pyproject_sync.py (same
    # pre-commit chain) blocks any commit whose pyproject.toml states a version
    # disagreeing with the authoritative plugin.json, and instructs you to "set
    # each pyproject.toml version equal to it and stage the result". Staging
    # exactly that result is a change under plugins/<name>/ with no version
    # CHANGE in plugin.json, which this gate then rejected -- so the sanctioned
    # fix for one gate was unlandable through the other. The historical cost was
    # real: a8ad064 needed the escape hatch, and c4a7c1b burned unreal-kit
    # 0.11.7 + bootstrap 0.77.3 on a no-op re-bump to satisfy a check that had
    # nothing to catch.
    #
    # Deliberately narrow, so this cannot mask a real code change: the ONLY
    # staged path under plugins/<name>/ must be its pyproject.toml, and the
    # version it now states must already equal the authoritative plugin.json.
    # Anything else -- a code file alongside it, or a pyproject stating a
    # version plugin.json does not -- still requires a bump. If either version
    # fails to parse the result is empty and we fall through to blocking, so the
    # failure direction stays conservative.
    staged_here=$(printf '%s\n' "$staged" | grep -E "^plugins/$name/" || true)
    if [ "$staged_here" = "plugins/$name/pyproject.toml" ]; then
        py_version=$(staged_pyproject_version "$name")
        pj_version=$(staged_plugin_json_version "$name")
        if [ -n "$py_version" ] && [ "$py_version" = "$pj_version" ]; then
            continue
        fi
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
