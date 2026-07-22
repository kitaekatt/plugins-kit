# Contributing to plugins-kit

plugins-kit is the development repository (source of truth) for a Claude Code
plugin marketplace. It ships a set of plugins that extend Claude Code with
skills, commands, and hooks, all riding on a shared dependency-provisioning
layer (the `bootstrap` plugin).

This guide is the human-facing entry point for working on the repo: layout,
setup, how plugins are structured, how to test, and how releases happen. It is
mechanism-first and points at the authoritative source for each procedure
rather than duplicating it.

The `CLAUDE.md` files throughout this repo are Claude-facing runtime context
(dense, rule- and gotcha-oriented, loaded into the agent automatically);
`README.md` and this `CONTRIBUTING.md` are the human surface. Where a `CLAUDE.md`
or a script owns a procedure, this file states the essentials and links to the
owner, which stays the single source of truth.

## Repository layout

```
plugins-kit/
  .claude-plugin/marketplace.json   # Marketplace manifest (lists published plugins)
  plugins/                          # One directory per plugin (the actual work)
    <plugin>/
      .claude-plugin/plugin.json    # Per-plugin manifest (name, version, deps)
      bootstrap.json                # Optional: dependency/provisioning manifest
      pyproject.toml                # Optional: Python dependencies
      skills/                       # SKILL.md files, discovered by Claude Code
  scripts/                          # Cross-plugin tooling (publish, regen, gen, checks)
  tests/                            # Test suites, mirroring plugins/ layout
  docs/                             # Reference, planning, and historical docs
```

`marketplace.json` is **derived data** -- it is regenerated from each plugin's
`plugin.json` by `scripts/regen_marketplace.py` and filtered by the
`"published"` field (missing means published; `false` excludes the plugin).
Never hand-edit its plugin entries; a pre-commit hook rejects drift.

## Development setup

Python tooling uses [uv](https://docs.astral.sh/uv/). The repo is pinned to
**Python 3.12** via a repo-root `.python-version`, so bare `uv run` / `uv venv`
select 3.12 everywhere; you do not pass `-p 3.12`.

You do not manually create venvs or `pip install` anything for the plugins
themselves -- the bootstrap plugin provisions each plugin's venv at session
start (see "How plugins are structured" below). `uv` at the repo root is only
for running the test suite and repo scripts.

### Running tests

The full suite is slow. Run only the file(s) relevant to your change:

```bash
# A specific test file
uv run --extra dev pytest tests/bootstrap/test_marketplace_lifecycle.py -v

# A whole plugin's tests
uv run --extra dev pytest tests/bootstrap -q
```

Only run the full suite (`uv run --extra dev pytest -v`) when explicitly asked
or right before a release. Test directories mirror the plugin structure:
`tests/<plugin>/` holds the tests for `plugins/<plugin>/`, and repo-level
tooling is tested under `tests/repo-scripts/`.

**Gotcha for p4-kit / git-kit tests.** Their review scripts re-exec themselves
under the plugin venv (`reexec_under_plugin_venv`, see below). On a machine
where that venv is provisioned, importing the script during test collection
calls `os.execv` and abandons the pytest process itself -- collection exits 0
with nothing run, a false green. Set the guard env var so the re-exec is a
no-op:

```bash
_BOOTSTRAP_GUARD_VENV_REEXEC=1 uv run pytest tests/p4-kit -q
```

## How plugins are structured

Each plugin follows the Claude Code plugin spec:

- **`.claude-plugin/plugin.json`** -- the plugin manifest: name, version,
  description, keywords, and `dependencies`. Every plugin that has a
  `bootstrap.json` declares `"dependencies": ["bootstrap"]` (a bare string,
  because bootstrap lives in the same marketplace). Plugins with no
  `bootstrap.json` do not declare it.
- **`bootstrap.json`** (optional) -- the provisioning manifest read by the
  bootstrap engine: system tools, git deps, and the plugin's venv requirements
  (`"venv": { "check_imports": [...] }`).
- **`pyproject.toml`** (optional) -- the plugin's actual Python dependencies,
  installed into its venv by the bootstrap engine using `uv`.
- **`skills/<name>/SKILL.md`** -- skills, discovered automatically by Claude
  Code.

### Dependencies: never install by hand

Plugin Python dependencies are declared, not installed manually. Add the
dependency to the plugin's `pyproject.toml` **and** add the import to
`bootstrap.json`'s `venv.check_imports` (the two go together: `pyproject.toml`
drives the install, `check_imports` tells the engine what to verify). The
bootstrap engine then creates a venv at a stable, version-independent path and
installs into it:

```
Windows:     ~/.claude/plugins/data/<marketplace>/<plugin>/.venv/Scripts/python.exe
macOS/Linux: ~/.claude/plugins/data/<marketplace>/<plugin>/.venv/bin/python
```

Never run `pip`, `python -m venv`, or any package manager manually for a
plugin's deps -- that lands packages in the wrong place and confuses the
engine's cache.

### Shared libraries and the bootstrap_guard discipline

A plugin can share a library (e.g. `bootstrap_lib`) with other plugins by
declaring it in `bootstrap.json` (`"shared_lib_imports": [...]`); bootstrap
links it onto the plugin's venv via a `.pth` file. Consequence: the shared lib
is importable **only** under the provisioned venv, not under a bare `python` or
`uv run`.

Two rules follow from this, both owned by
[`plugins/CLAUDE.md`](plugins/CLAUDE.md):

1. **Re-exec under the plugin venv.** A standalone script that hard-imports a
   shared lib must call `reexec_under_plugin_venv("<plugin>")` at module top,
   before the import. Skills name scripts with no interpreter, so an agent may
   run them under the wrong Python; the re-exec makes the script
   invocation-agnostic.
2. **`bootstrap_guard.py` is vendored byte-for-byte.** It is stdlib-only (so it
   can run when `bootstrap_lib` itself is absent) and must never import
   `bootstrap_lib`. The canonical copy lives at
   `plugins/bootstrap/bootstrap_lib/bootstrap_guard.py`; each consuming plugin
   ships an identical copy next to its script. Edit the canonical, then copy it
   into every vendored location -- `tests/bootstrap/test_bootstrap_guard.py`
   asserts the copies match.

## Local validation before publishing

Smoke-test the working copy before shipping. `--plugin-dir` loads a plugin
directly from disk (no cache, reverts on exit):

```bash
claude --plugin-dir ~/Dev/plugins-kit/plugins/my-plugin
```

**Blind spot:** `--plugin-dir` validates a plugin's *code* (skills, hooks,
engine), but the bootstrap engine still reads every plugin's `bootstrap.json`
from its cached install path, not from disk. So `--plugin-dir` does **not**
exercise new `bootstrap.json` content (added tools, `download:` recipes, new
`check_imports`). When your change touches manifest content, validate in
dev-tree mode instead (`scripts/dev-tree.py` repoints install paths at the dev
tree so the engine loads manifests from disk too). The root
[`CLAUDE.md`](CLAUDE.md) documents both modes and the helper shells around them.

## Testing standards

Every new module or integration point must have corresponding tests in
`tests/` before the work is considered complete. Tests go in the directory that
mirrors what they cover: `tests/<plugin>/` for a plugin, `tests/repo-scripts/`
for cross-plugin tooling. CI (`.github/workflows/tests.yml`) runs the bootstrap
suite on Ubuntu and Windows for every push and PR to `dev` and `master`.

## The shared code-review library and generated skills

git-kit and p4-kit both provide a multi-agent pre-submit code review. They run
the **same** pipeline and differ only in the VCS front-half (git ranges vs. a
Perforce changelist). Two mechanisms keep them in sync, and both matter when
you edit either plugin:

- **Shared back-half.** The VCS-neutral logic (diff chunking, CLAUDE.md
  gathering, submit-gate handling, the declined-ledger) lives in
  `plugins/bootstrap/bootstrap_lib/code_review/` and is imported by both
  plugins. Fix a shared behavior there, not in one plugin's script.
- **Template-generated skills.** The two `SKILL.md` files and their
  `references/submit-gates.md` (and related reference files) are **generated**
  from a single template by `scripts/gen_code_review_skills.py`, with a per-VCS
  substitution table. Do **not** hand-edit a generated skill file -- edit the
  template/fragments in the generator, then regenerate and commit all rendered
  files together:

  ```bash
  uv run python scripts/gen_code_review_skills.py            # rewrite the rendered files
  uv run python scripts/gen_code_review_skills.py --check    # exit 1 on drift, write nothing
  ```

  The drift guard runs in the test suite
  (`tests/bootstrap/code_review/test_skill_drift.py`) and asserts the committed
  output is byte-identical to what the template renders. Hand-editing a
  generated file will fail that test.

This code is deliberately shared through a library rather than by merging the
plugins -- plugin boundaries are hard boundaries in this repo, and cross-plugin
sharing is done through a library both depend on.

## Publishing

Do development on the **`dev`** branch. `master` is the consumer-facing cache
source: the Claude Code plugin cache syncs from `master`, and it keys on
version -- same version means same code, forever, from the cache's view. So a
change only reaches users through a real release.

**Publish only through the script. Never merge or push by hand.**

```bash
uv run python scripts/publish.py            # preflight, publish, verify
uv run python scripts/publish.py --check    # preflight + verify only; no writes
```

[`scripts/publish.py`](scripts/publish.py) is the source of truth for the flow
-- read its module docstring for the mechanics. "Publish" means **all four** of
these, atomically; anything less leaves consumers seeing something other than
what you meant:

1. Version bump (yours) + regenerated `marketplace.json`.
2. `index.html` regenerated from the dev tree, **inside** the release commit.
3. `dev` pushed.
4. `master` fast-forwarded and pushed.

The script owns everything derived from your commits and every git step after
them; you own the code and the version bump on `dev`.

### Version-bump rules

- Because the cache keys on version, **`plugin.json` and `marketplace.json`
  versions must move together** for any change you want consumers to receive.
- **Manifest edits count as code edits.** A `bootstrap.json` change (a new
  tool, a `download:` recipe, a new `check_imports`) without a version bump is
  invisible to consumers -- the engine keeps reading the old cached manifest.
  Bump the version.

### Dev-only plugins

Some plugins are in-development and must not reach consumers. Each sets
`"published": false` in its `plugin.json`; the marketplace regenerator filters
them out of `marketplace.json` structurally. `publish.py`'s preflight refuses
to publish when `dev` holds commits touching a dev-only plugin, naming them.

### The #1 hazard: do not fast-forward dev into master by hand

`dev` typically carries in-flight work from several plugins. **A manual
fast-forward `dev` -> `master` ships everything between the two branches, not
just your feature** -- unrelated WIP and dev-only plugins included. This is the
repo's single biggest publishing hazard.

The correct model is: `publish.py` owns the merge and refuses when it is unsafe.
When `dev` holds unrelated work, the fast-forward is not the happy path -- you
branch from `master`, cherry-pick only the publish-ready commits, and open a PR.
Before any merge, always check what would ship:

```bash
git fetch origin
git log --oneline origin/master..origin/dev
```

If that list contains anything beyond the commits you intend to publish, stop
and cherry-pick from `master` instead. Full mechanics, recovery from a botched
publish, and the dev/master reconcile policy live in the root
[`CLAUDE.md`](CLAUDE.md) and
[`docs/reference/publish-reconcile.md`](docs/reference/publish-reconcile.md).

## Where deeper docs live

- **`docs/reference/`** -- cross-plugin reference: the Claude Code plugin
  platform contract and the publish/reconcile procedures.
- **Per-plugin `README.md`** -- what each plugin does and how to use it (human
  surface).
- **Per-plugin `CLAUDE.md` and `skills/*/references/`** -- Claude-facing runtime
  context and detailed skill internals. These are the source of truth for how
  the systems work; `README`/`CONTRIBUTING` point at them rather than
  duplicating them.
- **Root [`CLAUDE.md`](CLAUDE.md)** -- the authoritative account of the
  development workflow, publishing rules, gotchas, and validation modes
  summarized above.
