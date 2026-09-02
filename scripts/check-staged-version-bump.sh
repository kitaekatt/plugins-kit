#!/bin/sh
# check-staged-version-bump.sh -- Block commits that leave a plugin's version
# equal to the version it had at the last publish point while its files change.
#
# Why: the repo's most-documented failure mode is "code changed but version
# didn't" (silent divergence / burned versions / invisible manifest edits --
# see root CLAUDE.md gotcha 3 and the manifest_changes_need_version_bump
# insight). This check turns that from a remembered rule into an enforced one.
#
# THE QUESTION IS ABOUT THE END STATE, NOT ABOUT THIS COMMIT'S DIFF. For each
# plugin with staged changes: does its version IN THE INDEX differ from its
# version at the last publish point? Asking instead whether this commit's own
# staged diff moves the version line is a question about authoring mechanics,
# and it gives wrong answers that have nothing to do with the invariant:
#
#   * `git commit --amend` was refused outright. Index and HEAD both already
#     carry the bump, so `git diff --cached` shows no version change -- for a
#     commit that demonstrably contains one. The escape hatch below was then
#     the only way through, which trained the habit of reaching for it on a
#     check that would have passed.
#   * Splitting one change over several commits, bumping in the first, was
#     refused from the second commit onward for the same reason.
#   * A plugin edited across several commits where NONE carries a bump passed
#     nothing and was caught by nothing -- the per-commit question cannot see
#     it, and this is a real unbumped change reaching a publish.
#
# The publish point comes from publish.py's range_base(), invoked rather than
# reimplemented: it searches DOWN master's history for a `Published-From:`
# trailer (master carries non-release commits, so the tip is not the boundary)
# and falls back to origin/master. publish.py's own preflight measures bumps
# from the same function, so this gate and the unbypassable one agree by
# construction instead of by two implementations staying in step.
#
# WHAT THIS DELIBERATELY NO LONGER CATCHES: once a plugin is bumped since the
# last publish point, subsequent commits touching it pass with no further bump.
# That is weaker per commit, and correct -- consumers key on version, and one
# bump since the last publish is exactly what makes them refetch.
#
# Escape hatch (still needed: a dev commit on a plugin genuinely not intended
# to ship in the next publish, and any degraded case below):
#     PLUGINS_KIT_SKIP_BUMP_CHECK=1 git commit ...
# or bypass all hooks with `git commit --no-verify`.
#
# DEGRADED MODE. On an unprovisioned clone the publish point may be
# undiscoverable -- no Python, no origin/master, publish.py absent. The gate
# then says so on stderr and falls back to the per-commit staged-diff question,
# which needs nothing but git. Every failure direction here blocks rather than
# passes: an unparseable version, an unresolvable base, a plugin whose index
# version cannot be read.
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
version_of_stream() {
    sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1
}

staged_pyproject_version() {
    git show ":plugins/$1/pyproject.toml" 2>/dev/null \
        | sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1
}

staged_plugin_json_version() {
    git show ":plugins/$1/.claude-plugin/plugin.json" 2>/dev/null | version_of_stream
}

# --- the publish point -----------------------------------------------------
#
# Resolved once. Interpreter resolution mirrors pre-commit-version-check.sh
# (venv first, then whatever is on PATH); that script does not export its own
# choice, and re-deriving it is cheaper than coupling the two by an env var.
plain_python=""
for candidate in ".venv/bin/python" ".venv/Scripts/python.exe"; do
    [ -x "$candidate" ] && { plain_python="$candidate"; break; }
done
if [ -z "$plain_python" ]; then
    for candidate in python3 python py; do
        if command -v "$candidate" >/dev/null 2>&1; then
            plain_python="$candidate"; break
        fi
    done
fi

base=""
degraded_reason=""
if [ -z "$plain_python" ]; then
    degraded_reason="no Python 3 found"
elif [ ! -f "scripts/publish.py" ]; then
    degraded_reason="scripts/publish.py not present"
else
    base=$("$plain_python" scripts/publish.py --print-range-base 2>/dev/null || true)
    if [ -z "$base" ]; then
        degraded_reason="publish.py could not report the publish point"
    elif ! git rev-parse --verify --quiet "$base^{commit}" >/dev/null 2>&1; then
        # range_base() falls back to the literal string origin/master, which an
        # unfetched or origin-less clone does not resolve. Not an error there --
        # just nothing to measure against.
        degraded_reason="publish point '$base' does not resolve in this clone"
        base=""
    fi
fi

if [ -n "$degraded_reason" ]; then
    echo "check-staged-version-bump: $degraded_reason; falling back to the" >&2
    echo "  per-commit staged-diff check (weaker: it cannot see a bump made in" >&2
    echo "  an earlier commit, so an amend or a split change may be refused)." >&2
fi

missing=""
for name in $names; do
    pj="plugins/$name/.claude-plugin/plugin.json"
    # Plugin dir being deleted entirely (or not a real plugin dir): nothing to bump.
    [ -f "$pj" ] || continue

    if [ -n "$base" ]; then
        # End state: the version this commit will leave in the tree, against the
        # version the last publish shipped. A plugin that does not exist at the
        # base is new, so there is nothing it could fail to differ from -- and
        # base_version stays empty, which an existing version already differs
        # from. An index version that will not parse leaves index_version empty
        # and falls through to blocking, deliberately.
        index_version=$(staged_plugin_json_version "$name")
        base_version=""
        if git cat-file -e "$base:$pj" 2>/dev/null; then
            base_version=$(git show "$base:$pj" 2>/dev/null | version_of_stream)
        fi
        if [ -n "$index_version" ] && [ "$index_version" != "$base_version" ]; then
            continue
        fi
    else
        # Degraded: the original per-commit question.
        if git diff --cached -U0 -- "$pj" | grep -Eq '^[+-].*"version"[[:space:]]*:'; then
            continue
        fi
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
    echo "Staged changes touch plugin(s) not bumped since the last publish point:"
    for name in $missing; do
        echo "  - $name (plugins/$name/.claude-plugin/plugin.json version unchanged)"
    done
    echo ""
    echo "Published consumers only refetch a plugin when its version changes;"
    echo "code or manifest edits without a bump are invisible to them (root"
    echo "CLAUDE.md gotcha 3). Bump the version and run"
    echo "\`python scripts/regen_marketplace.py\`, or -- for a dev-branch commit"
    echo "on a plugin not meant to ship in the next publish -- bypass with:"
    echo "  PLUGINS_KIT_SKIP_BUMP_CHECK=1 git commit ...   (or git commit --no-verify)"
} >&2
exit 1
