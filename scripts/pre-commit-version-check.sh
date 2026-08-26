#!/usr/bin/env bash
# pre-commit-version-check.sh -- the repo's commit-time consistency checks.
#
# LAYERING, stated once so the next check gets added to the right place:
#   * Leak checks gate COMMITS. `dev` is pushed freely to a PUBLIC repo, so
#     anything committed is world-readable immediately and un-publishing it from
#     history is the force-push nightmare CLAUDE.md forbids. That is
#     precommit_guard.py, run separately by .githooks/pre-commit, and it is
#     deliberately unforgiving.
#   * Consumer-correctness checks (this file) gate PUBLISHES. They protect
#     people who install from master; nothing here can hurt anyone until a
#     publish happens. publish.py's preflight enforces these invariants
#     unbypassably. What runs HERE is therefore advice delivered while the author
#     still has the context to act on it cheaply -- not the gate.
# Everything below follows from that: these checks are escapable, are scoped to
# what the commit actually contains, and must never block work they do not own.
#
# SCOPED TO THE COMMIT, NOT THE TREE. This working tree is shared with
# concurrent agent sessions. A check that reads the WORKTREE fails on edits the
# commit does not contain -- one session's in-flight version bump used to block
# every other session's commits -- and it also passes an inconsistent pair that
# IS staged, since history is built from the index. So a check here judges the
# index and returns success when the commit stages none of its inputs.
#
# Install: ln -sf ../../scripts/pre-commit-version-check.sh .git/hooks/pre-commit

set -uo pipefail   # NOT -e: see "collect, then report" below.

# Resolve via git (works whether the hook is installed as a symlink, a copy, or
# wired via core.hooksPath). Falling back to $0-relative resolution would give
# the wrong directory when the hook is a symlink at .git/hooks/pre-commit.
REPO_ROOT="$(git rev-parse --show-toplevel)"

# Interpreter resolution, split by what each check actually needs.
#
# The checks in this file are stdlib-only and run under a plain interpreter --
# fast, with no venv sync, and usable on an unprovisioned clone.
PLAIN_PYTHON=""
for candidate in \
    "$REPO_ROOT/.venv/bin/python" \
    "$REPO_ROOT/.venv/Scripts/python.exe"; do
    [ -x "$candidate" ] && { PLAIN_PYTHON="$candidate"; break; }
done
if [ -z "$PLAIN_PYTHON" ]; then
    for candidate in python3 python py; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PLAIN_PYTHON="$candidate"; break
        fi
    done
fi
if [ -z "$PLAIN_PYTHON" ]; then
    echo "pre-commit: no Python 3 found; consistency checks SKIPPED." >&2
    echo "pre-commit: the public-repo leak guard still ran." >&2
    exit 0
fi

# Collect, then report. Under `set -e` the first failing check aborted the rest,
# so a tree with two problems took two commit attempts to discover -- worse in a
# shared tree, where a foreign in-flight state can trip a check you do not own.
# Every check now runs and every failure is printed; the exit code is the OR.
failed=0
note() { printf '%s\n' "$*" >&2; }

# --- marketplace.json is derived data -------------------------------------
# Rebuilt from each plugins/<name>/.claude-plugin/plugin.json by
# regen_marketplace.py. Hand-editing it is not the workflow.
if ! "$PLAIN_PYTHON" "$REPO_ROOT/scripts/regen_marketplace.py" --check --staged; then
    note ""
    note "Bump versions / descriptions in plugins/<name>/.claude-plugin/plugin.json,"
    note "run \`python scripts/regen_marketplace.py\`, stage the result, and commit."
    failed=1
fi

# --- staged plugin changes need a staged version bump ----------------------
# The cache keys on version, so shipping changed code under an unchanged version
# is invisible to consumers forever (CLAUDE.md gotcha 3).
# Escape hatch: PLUGINS_KIT_SKIP_BUMP_CHECK=1, or --no-verify.
if ! "$REPO_ROOT/scripts/check-staged-version-bump.sh"; then
    failed=1
fi

# --- pyproject.toml must agree with plugin.json ---------------------------
# Companion to the gate above: that one forces plugin.json to be bumped, and
# nothing pulled pyproject.toml along with it -- which is how bootstrap's stated
# version drifted across five releases while a test that would have caught it
# sat un-run. Same escape hatch.
if ! "$PLAIN_PYTHON" "$REPO_ROOT/scripts/check_pyproject_sync.py" --staged; then
    failed=1
fi

# --- a plugin shipping bootstrap.json must depend on bootstrap ------------
# Otherwise a user can install it without bootstrap and its manifest is never
# processed (CLAUDE.md, "Plugin dependencies on bootstrap"). Same escape hatch.
if ! "$PLAIN_PYTHON" "$REPO_ROOT/scripts/check_bootstrap_dependency.py" --staged; then
    failed=1
fi

# --- instructions we ship to Claude must be checkable ---------------------
# Text under plugins/ reaches a consumer's session, some of it through the same
# channel that carries untrusted content, so an unbacked claim of authority is
# indistinguishable from an attack (docs/reference/agent-directive-standards.md).
# This covers only the greppable subset; the standard is judgment work. It is
# here because the judgment half demonstrably failed with the author's full
# attention -- the session that wrote the standard shipped a false claim inside
# the standard's own enforcement section.
if ! "$PLAIN_PYTHON" "$REPO_ROOT/scripts/check_agent_directives.py" --staged; then
    failed=1
fi

exit "$failed"
