# CLAUDE.md

## Project Overview

**plugins-kit** is the **development repository** (source of truth) for the plugins-kit Claude Code marketplace. It contains the source code for all plugins in the marketplace. Published plugins: **awesome-kit** (plugin-ecosystem poster, orchestrate, task tracking), **bootstrap** (dependency management), **bootstrap-stuck-fix** (temporary remediation shim for a wedged bootstrap registry record), **cache-kit** (cache-usage reporting from transcripts), **claude-ui-kit** (status line + /statusline), **content-pipeline-kit** (library + skills for LLM-in-the-loop batch content pipelines), **git-kit** (Git/GitHub multi-agent code review + gh bootstrap), **hue-kit** (Philips Hue layered-scene framework: bridge sync, YAML scenes, meta-group solver), **llm-scripting-kit** (LLM key resolution, shared model registry, and named OpenAI-compatible endpoints -- OpenRouter is the default endpoint; importable package `llm_scripting_kit`, CLI `llm-scripting-kit`), **p4-kit** (Perforce multi-agent code review), **pdf-kit** (HTML-to-PDF via headless Chromium), **secrets-kit** (fleet secrets provisioning), **skills-kit** (verb x artifact authoring/audit matrix for skills + CLAUDE.md, folded into a single domain-skill: /md-domain, plus knowledge-encoding, update-documentation, materialized-output), **unreal-kit** (Unreal Engine Python API automation), **workflow-kit** (declarative .workflow.yaml compiler and node strategies), and **job-kit** (durable sequential agent-job runner). Dev-only (not published, `published: false`): **prototypes** (inactive experimental nursery/archive), **yaml-data-editor-kit**.

This repo is a **Claude Code plugin marketplace** -- it extends Claude Code with skills, commands, and hooks via the `.claude-plugin/marketplace.json` manifest. Plugins are loaded either via `--plugin-dir` (local development) or `enabledPlugins` in settings (production installs from the remote repo).

## Harness ownership

Develop plugin features **Claude-first** in this repository. Establish the Claude Code skill, command, hook, or plugin-root invocation before you adapt the reusable seam.

The sibling `../codex-plugins-kit` repository owns thin Codex-first adapters and its `AGENTS.md`. Reusable behavior stays here. Codex-specific packaging stays there.

## Architecture

```
plugins-kit/                          # Marketplace root
  .claude-plugin/marketplace.json     # Marketplace manifest (lists all plugins)
  plugins/
    bootstrap/                        # Bootstrap plugin (always enabled)
      .claude-plugin/plugin.json      # Plugin manifest
      bootstrap.json                  # Bootstrap plugin's own manifest
      engine/                         # Bootstrap engine + config
      bootstrap_lib/                  # Shared libraries (tool_check, venv_check, etc.) -- installable Python package
      hooks/sessionstart/             # SessionStart hook (bash wrapper)
      defaults/                       # Default config files
    p4-kit/                           # P4 multi-agent code review plugin (Claude subagents)
      .claude-plugin/plugin.json      # Plugin manifest
      bootstrap.json                  # Bootstrap manifest (tools)
      scripts/prepare_review.py       # Diff + CLAUDE.md gathering (stdlib-only, called by skill)
      skills/p4-code-review/          # Multi-agent review skill (3 reviewers + per-issue validators)
    unreal-kit/                       # The UE plugin
      .claude-plugin/plugin.json      # Plugin manifest
      lib/                            # Shared Python libraries (synced to data dir by bootstrap)
      skills/
        ue-python-api/                # The main skill
          SKILL.md                    # Skill definition (loaded by Claude Code)
          scripts/                    # Entry points (ue_runner.py + ue-runner.cmd) + utility scripts
          stubs/                      # UE Python API stubs (generated, gitignored)
          references/                 # Detailed docs loaded conditionally by SKILL.md
```

### Key Files

| File | Purpose |
|------|---------|
| `plugins/bootstrap/engine/bootstrap_engine.py` | Thin CLI wrapper (73 lines) that contains any `ImportError` from `bootstrap_lib.engine` and delegates to it -- the real engine (manifest processing, checks, hook JSON) lives in `plugins/bootstrap/bootstrap_lib/engine.py` |
| `plugins/bootstrap/bootstrap_lib/tool_check.py` | System tool availability checks |
| `plugins/bootstrap/bootstrap_lib/platform_detect.py` | OS detection |
| `plugins/bootstrap/bootstrap_lib/log.py` | File-based bootstrap logging |
| `plugins/bootstrap/bootstrap_lib/venv_check.py` | Python venv validation |
| `plugins/bootstrap/bootstrap_lib/git_dep_check.py` | Git dependency validation |
| `plugins/bootstrap/bootstrap_lib/plugin_resolve.py` | Plugin registry resolution |
| `plugins/bootstrap/bootstrap_lib/path_check.py` | PATH entry validation |
| `plugins/bootstrap/bootstrap_lib/manifest_merge.py` | Deep-merge for layered bootstrap.json files |
| `plugins/bootstrap/bootstrap_lib/agent_skills_check.py` | `agent_skills_link` check/fixer: links `<project>/.agents/skills` to `<project>/.claude/skills` for Codex, incl. Git/P4 exclusion |
| `plugins/bootstrap/engine/config.py` | Thin re-export shim (11 lines, `from bootstrap_lib.config import *`) kept for callers importing bare `config` via the `plugins/bootstrap/engine` pythonpath entry (e.g. `tests/bootstrap/test_config.py`) -- the real config loading, migration, and persistence live in `plugins/bootstrap/bootstrap_lib/config.py` |
| `plugins/bootstrap/hooks/sessionstart/session-bootstrap.sh` | SessionStart hook (bash wrapper for engine) |
| `plugins/bootstrap/bootstrap.json` | Bootstrap plugin's own manifest |
| `plugins/bootstrap/skills/bootstrap/references/engine-internals.md` | Bootstrap engine internals |
| `docs/reference/shared-lib-architecture.md` | Six findings about the `shared_libs` mechanism (standalone-broadcast collisions, verify-after-write, no revocation, no version marker, `sys.modules` staleness, a duplicated standalone-interpreter path), reviewed 2026-09-20; five open, verify-after-write fixed in bootstrap 0.123.0 |
| `docs/planning/bootstrap/MILESTONES.md` | Development milestones and progress |
| `docs/reference/adapters.md` | Current adapter guidance: model-task selection, corpus independence, admission, artifact contract, and task-skill ownership at the emitter |
| `docs/reference/adapters-negative-results.md` | Closed adapter experiments, disqualified candidates, cost rejections, and explicitly unmeasured deferrals |
| `docs/planning/adapters/adapter-design.md` | Historical md-audit adapter design record and measurement tables; the task skill owns the adapter and enforces it at the emitter |
| `tests/bootstrap/` | All bootstrap tests (mirrors bootstrap_lib/ structure) |

### Key Design Decisions

- **Bootstrapping**: Two-layer system -- session bootstrap (bash SessionStart hook, manifest-driven) ensures system tools, venv, and git deps; script bootstrap (Python, runs inside UE Editor) handles UE-side packages at runtime. See [engine-internals.md](plugins/bootstrap/skills/bootstrap/references/engine-internals.md) for engine details and [script-bootstrap.md](plugins/unreal-kit/skills/ue-python-api/references/script-bootstrap.md) for UE-side bootstrapping.

### Unreal Engine work

For UE Python automation (running scripts, the `ue_runner` host-side runner, in-editor patterns, sys.path conventions, dependency bootstrap), invoke the `ue-python-api` skill at `plugins/unreal-kit/skills/ue-python-api/SKILL.md`.

## Bootstrap (foundation for all plugins)

The **bootstrap** plugin is the dependency-management layer every other plugin in this marketplace rides on. Claude Code runs it via a SessionStart hook at the start of every session; bootstrap then reads each enabled plugin's `bootstrap.json` and ensures system tools, venvs, git deps, marketplaces, and per-user config are in the state the plugins need. **No bootstrap, no working plugins.** When `bootstrap.json`, `pyproject.toml`, or `uv.lock` changes, a bootstrap pass detects the change and applies or remediates it.

**Healthy bootstrap is silent.** No SessionStart output does NOT mean bootstrap is broken; it means every check passed (or hit a cache). To verify a plugin's bootstrap actually ran, read its log at `~/.claude/plugins/data/<marketplace>/<plugin>/bootstrap.log`. If the log doesn't exist, bootstrap never reached that plugin -- most often because the per-project cooldown short-circuited the run (see below).

**Per-project cooldown.** After a pass, bootstrap writes a per-project timestamp (`~/.claude/plugins/data/plugins-kit/bootstrap/cooldowns/last_run_epoch.<sha1-of-cwd>`); SessionStart hooks inside the window are skipped entirely -- no checks, no logs, no remediation. It is the only run throttle, and the answer ~99% of the time when bootstrap appears to be ignoring you. Reset it with:

```bash
bash plugins/bootstrap/scripts/bootstrap-reset-cooldown.sh             # current project (CWD)
bash plugins/bootstrap/scripts/bootstrap-reset-cooldown.sh --all       # every project
bash plugins/bootstrap/scripts/bootstrap-reset-cooldown.sh --status    # list cooldowns + ages, no writes
bash plugins/bootstrap/scripts/bootstrap-reset-cooldown.sh --clear-alerts  # also nuke pending alert/display files
```

The reset script's `--help` is the canonical doc.

**Update-lifecycle guardrails** (the bootstrap skill is the SSOT -- invoke `/bootstrap`, facts `message_outcomes` + `update_lifecycle`, and `plugin-reload-lifecycle.md` for full mechanics):

- A published update converges in ONE session without a manual reset -- both skip gates (session-id guard, cooldown) auto-bypass on a registry change, `claude --resume` included, and the harvest runs the new engine in-session. Do not prescribe extra restarts or resets on a normal publish.
- A manual `bootstrap-reset-cooldown` is needed only after editing a *layered* `bootstrap.json` (`~/.claude/` or `<project>/.claude/`) or a project `pyproject.toml` / `uv.lock`, none of which touch a registry file.
- Provisioning is done once `engine_ran_version` == installed version; a restart is then needed only to load new plugin CODE. `engine_ran_version` staying behind after a restart + a prompt is an **anomaly to surface**, not success.
- Mid-session installs (`/plugin` + `/reload-plugins`) also converge without a restart -- a fresh plugin's venv exists a prompt or two later.

### Anti-pattern: repairing a wedged machine by hand

**Our job is not to fix bootstrap issues. It is to make bootstrap fix them.** A machine that is wedged is a *specification* for a repair that ships; it is not a chore to clear. Whenever you find yourself typing the command that unwedges the machine in front of you, you are writing the wrong artifact.

A hand-repair fails twice over:

1. **It converges nobody.** The machine in front of you is one of N. Every other user with the same wedge is still stuck, and nothing you did will reach them. Only a published change to `bootstrap` (or `bootstrap-stuck-fix`, per the escape-hatch test below) reaches anyone.
2. **It destroys the evidence.** A wedge is usually only observable while it is happening. Repairing it overwrites the registry, the cache tree, and the marketplace clone -- the exact state needed to diagnose it. A hand-repair therefore converts a diagnosable defect into a permanently unexplained one, guaranteeing the recurrence it appeared to resolve.

The second cost is the one that gets underestimated, because the machine looks *better* afterwards. It isn't. A healthy machine with an unknown root cause is strictly worse than a wedged machine you can still read.

**Both entry points are the anti-pattern when a machine is wedged or being diagnosed.** This applies equally to the engine (`bootstrap_engine.py`) and to the hook (`plugins/bootstrap/hooks/sessionstart/session-bootstrap.sh`). Hand-invoking either on a wedged machine runs a full live pass outside the conditions bootstrap is designed for, so what you observe generalizes to nobody. The one sanctioned manual run is post-update convergence on a HEALTHY machine -- the bootstrap skill's `manual_convergence` fact (`bootstrap run`, which runs the SessionStart engine pass exempt from both skip gates); it converges, it does not diagnose, and it is never the answer to a wedge. (An earlier insight in this file, `run_bootstrap_hook_directly`, advised the opposite; it is retracted and replaced by `never_run_bootstrap_hook_directly`.)

**The legitimate way to make bootstrap run again on a wedged machine** is to clear the throttle and let a real session start do the work:

```bash
bash plugins/bootstrap/scripts/bootstrap-reset-cooldown.sh   # then start a session
```

That exercises the same code path under the same conditions a user gets, so the result means something. Resetting the cooldown is not the anti-pattern -- forcing the pass yourself to diagnose a wedge is; the healthy-machine convergence run named above is the one exception.

**The discipline.** When a machine is wedged:

1. **Snapshot first, always.** Capture `installed_plugins.json` verbatim, the `~/.claude/plugins/cache/` tree listing, each marketplace clone's HEAD sha and `git status`, `enabledPlugins` from user and project settings, and the Claude Code version. This costs seconds and is the only artifact that survives the repair.
2. **Then write the repair into the plugin**, choosing `bootstrap` vs `bootstrap-stuck-fix` by the escape-hatch test below.
3. **Let the mechanism heal the machine.** The wedged machine is the integration test for the repair. Unwedging it by hand forfeits that test.

A hand-run engine invocation is a diagnostic of last resort, valid only *after* step 1 -- and even then, prefer a read-only probe over a full pass.

**`--console` is not read-only.** It suppresses log-file writes and JSON output; it does **not** suppress provisioning. A `--console` pass still fetches marketplaces, installs plugin versions into the cache, and rewrites `installed_plugins.json`. Do not reach for it as a safe way to look at a wedged machine -- it is a live pass with quieter output. (Misreading `--console` as read-only is what turned a 2026-07-27 wedge investigation into a repair -- nine plugins reported `not cached`, the engine was hand-invoked, and the failing state was destroyed before it could be diagnosed; see the `never_hand_repair_a_wedge` insight.)

**Bootstrap cannot patch itself.** A delivery-path bug (update, harvest, registry record selection, install scope) is remediated in `plugins/bootstrap-stuck-fix/`, not by a bootstrap release. Such bugs self-mask as one stable error. Test and rationale: `/bootstrap` fact `update_lifecycle`. Covered defects and the narrowness discipline: that plugin's README.

For deeper material -- manifest schema, condition categories, fix-all flow, engine internals -- invoke `/bootstrap`.

## Development Workflow

**Automated tests required** -- every new module or integration point must have corresponding tests in `tests/` before the work is considered complete. Test directories mirror the plugin structure (e.g. `tests/bootstrap/` for the bootstrap plugin). This standard was established with the bootstrap plugin's M1 test suite and applies to all subsequent development.

**A check must be shown to fail.** Before believing a test or guard protects
something, remove what it protects and watch it go red -- revert the fix and
run the named test; for a guard that compares a generated artifact to its
generator, ask what happens when both move together. A check that stays green
is worse than no check, because the green result stops anyone looking again.
Both observed shapes, their worked examples, and the remedy:
[docs/reference/vacuous-checks.md](docs/reference/vacuous-checks.md).

**Targeted test runs** -- the full test suite is too slow for routine use. Always run only the specific test file(s) relevant to your changes:

```bash
# Run a specific test file
uv run --extra dev pytest tests/bootstrap/test_marketplace_lifecycle.py -v

# Run a specific test class
uv run --extra dev pytest tests/bootstrap/test_marketplace_lifecycle.py::TestCheckPluginScope -v
```

Only run the full suite when explicitly asked or before a release -- and when you do,
**parallelise it**, because the full suite is the only run where that pays:

```bash
uv run --extra dev pytest -n 12 -q      # full suite, ~3 min
```

`-n` is deliberately not in `addopts`: worker startup is a fixed toll that is free on the full run and ruinous on a targeted one. Full suite: `-n 12`, ~3 min. Leak guards are only complete in a SERIAL run. **A test that fails only under `-n` is usually load, not your change -- and never write a bare `time.sleep` sized for an idle machine; poll for a causal observable instead.** Measurements, the worker-count table, the timing-sensitivity trap and the leak-guard explanation: [docs/reference/testing.md](docs/reference/testing.md).

**Interpreter: the repo is pinned to Python 3.12** via a repo-root `.python-version`, so bare `uv run` / `uv venv` select 3.12 everywhere -- no `-p 3.12` needed. Nothing needs 3.14 (four plugins exclude it: `requires-python ">=3.12,!=3.14.*"`); it used to leak in only as uv's global default when no pin was present.

The two formerly-documented "pre-existing failure" clusters (the `tests/skills-kit/` collection errors and the bootstrap `engine`/`venv` `CalledProcessError`s) were **fixed**, not version quirks -- both were test-only issues: skills-kit imported the pre-extraction `schemas`/`_shared` modules, and the bootstrap tests spawned WSL `bash` to `source` a Windows env file and didn't isolate `HOME`. **The suite is not unconditionally green, and "green" is host-dependent.** On an arm64 machine (Apple Silicon) five `tests/bootstrap/test_manifest_normalization.py` scoop tests failed for months while passing on every amd64 box, because they fake `current_os` but not `detect_arch()`, which reads the real CPU -- see the `suite_green_is_host_dependent` insight below. Establish a baseline on YOUR machine before calling a failure your regression: first try undoing your own edits for a moment and re-running the failing test; when that cannot answer it, run the suite at the merge-base in a read-only worktree and remove it afterwards (see "Worktrees and scratch copies").

If the failing test changes between runs it is a leak, not a flake -- find the
writer, not the victim; see [docs/reference/testing.md](docs/reference/testing.md).


**Local development** -- use `--plugin-dir` to test plugins from the working copy:

```bash
claude --plugin-dir ~/Dev/plugins-kit/plugins/my-plugin
```

`--plugin-dir` loads the plugin directly from disk (no cache copy) and makes no persistent changes -- it doesn't modify `installed_plugins.json`, the cache, or `known_marketplaces.json`. Ending the session reverts to the marketplace-installed version.

**Reload vs restart:** edits to hook/skill script content are live; `/reload-plugins` reloads registration in-session; only a SessionStart re-fire or a real version update needs a restart. Details: [plugin-reload-lifecycle.md](plugins/bootstrap/skills/bootstrap/references/plugin-reload-lifecycle.md).

**Publishing** is `uv run python scripts/publish.py` -- the only user-gated action in this repo, and the source of truth for the flow; do not hand-run its steps. Definition of a publish, `marketplace.json` as derived data, the commit-scoped pre-commit check, dev-only filtering, and `index.html` regeneration:
[docs/reference/publish-reconcile.md](docs/reference/publish-reconcile.md).

**Publication completeness is conditional on configuration.** A plugin is
correctly published when its manifest says `published: true` (or omits the
flag) and it is present in the release. A plugin is correctly unpublished when
its manifest says `published: false` and the publisher holds its files back.
Do not report an intentionally unpublished plugin as a publication gap.

Publishing is reversible-but-visible: nothing is destroyed, but it goes out to other machines. The bar is "user has expressed publish intent for this work," not "user has reconfirmed each git command." Treat unambiguous go-signals -- `go`, `ship it`, `publish`, `do it`, `close the loop`, `push` -- as authorizing the whole flow; run `publish.py` and let its preflight be the safety net. Confirm only when intent is genuinely ambiguous (partial work, no version bump in sight, unrelated WIP staged, or the user is mid-thought).

**Plugin `uv.lock` files are ignored and untracked; never commit one.** `.gitignore` lists `uv.lock`, and a tracked plugin lockfile records the plugin's own version, so every bump leaves it stale and the next `uv sync` dirties the tree, which blocks `publish.py`. A dirty `plugins/<name>/uv.lock` therefore means one was re-tracked: `git rm --cached` it. Do not commit the refreshed copy. When merging a branch that deleted one, resolve the modify/delete conflict as the deletion. The root `uv.lock` (this checkout's maintainer environment) is the one tracked exception.

**A publication hold on ONE plugin belongs here, not in a task folder.** A
release ships the whole range, so every publish from `dev` carries every
changed plugin; a hold that lives anywhere a publisher does not read binds
nobody. The recorded failure behind this rule: docs/reference/publish-reconcile.md
("Publication hold rationale").

Two ways to hold a plugin back that actually work: `"published": false` (see
"Dev-only plugins -- do not publish to master" below), or a hold named in THIS
file, in the section a publisher reads before running `publish.py`. A note
anywhere else is a record of an intention, not a gate.


After publish:

- Users with `autoUpdate: true` receive the update on next session start.
- Users without auto-update run `/plugin marketplace update` then `/plugin update`.

### Dev-only plugins -- do not publish to master

Some plugins live on `dev` for in-development work and must not reach consumers until they are ready. Each such plugin sets `"published": false` in its `plugins/<name>/.claude-plugin/plugin.json`. The marketplace regenerator (`scripts/regen_marketplace.py`) filters those plugins out of `marketplace.json`, so they are excluded structurally -- not by memory -- even if their files land on master via a cherry-pick.

**`published: false` governs the marketplace LISTING, not the source sync -- and the two are separately enforced.** The regenerator keeps the plugin out of `marketplace.json`, which is what makes it uninstallable. Separately, `_publish_projection` derives its dev-only set from the MANIFESTS rather than from any exclusion flag, so a dev-only plugin's FILES are held back on every projection: a path master already carries is restored to master's content, a path only dev has is removed. Its COMMITS still appear in the shipping list; only its files stand still.

The consequence to plan around: a dev-only plugin's copy on master is re-checked-out every release and can only go stale, never advance. Removing it from master once flips the hold-back into deleting it instead, permanently and with no flag. Verify a claim about this against `plugin.json` on both branches -- the field alone does not tell you whether source is on master.

`--exclude-dev-only <plugin>` does NOT control that file hold-back and cannot remove anything from master. It governs commit bookkeeping only: which commits land in `excluded`, the "Held back on dev" line in the projection commit message, and eligibility for the fast-forward shortcut. With an exclusion in force, preflight refuses one case -- a single commit touching **both** that plugin and files that would otherwise ship -- because an exclusion cannot be honoured silently there. Mechanics: [docs/reference/publish-reconcile.md](docs/reference/publish-reconcile.md).

**Dev-only plugins** (the field, not this list, is load-bearing -- this is just a human-readable inventory):

- **yaml-data-editor-kit** (source removed from master in `37fb94f6`; the hold-back keeps it off).
- **prototypes** (inactive experimental nursery/archive; ships no skills).

Commits for a dev-only plugin need no action -- the file hold-back handles them. See "Anti-pattern: creating a branch" for why not to route around them with one.

**`git branch --contains <sha>` cannot tell you whether your work shipped.**
Because a release is a tree PROJECTION rather than a merge, a dev commit's SHA
never appears on `master` even once its content is published. Asking git which
branches contain the commit therefore answers "only dev" for work that shipped
hours ago, which reads as proof the publish did not happen. Check CONTENT
instead -- `git show origin/master:<path>` for the change you made, or the
plugin's version in `origin/master:plugins/<name>/.claude-plugin/plugin.json`.
The same trap runs the other way through the plugin cache: a version present
under `~/.claude/plugins/cache/` was fetched from master, so its presence is
evidence of a publish that this repo's git graph will not show you.

### dev -> master reconcile: master-only content

A release projects dev's tree, so it never merges. When `publish.py` refuses because master holds content dev lacks, back-port the master-only `+` lines worth keeping to dev (on dev, in this folder), then publish normally. Full policy: [docs/reference/publish-reconcile.md](docs/reference/publish-reconcile.md).

### Pre-publish validation (default)

**Default gate: before any publish, smoke-test the dev working copy with `claudx`.** `claudx` is an alias for `claude-plugin-test` (defined in `~/.bashrc`), which runs [`scripts/claude_plugin_test.py`](scripts/claude_plugin_test.py). It launches a `claude` session with one `--plugin-dir` per plugin ENABLED for the project (`--all` loads the whole tree), so those plugins' skills, hooks and engine **code** load straight from disk, and it also makes the bootstrap engine read their **`bootstrap.json` from disk**. Run it, exercise the changed surface (invoke the skill, trigger the hook, run the command), confirm it behaves, then publish.

```bash
claudx                    # plugins enabled for this project: dev code AND dev manifests
claudx --all              # every plugin in the tree, ignoring enablement
claudx --fresh            # discard the dev data root first
claudx --print            # show the command, launch nothing
claudx -- -p "hello"      # pass args through to claude
```

`--project-dir` selects the project whose scoped settings decide enablement (default: cwd); `--data-root` relocates the session's bootstrap data.

**Enablement filtering is the false-pass trap.** A plugin not enabled for the cwd project is dropped with a `not enabled, skipping: <names>` note on stderr, and a run with nothing enabled exits `error: no enabled <marketplace> plugins for <dir>`. So smoke-testing a plugin that is disabled for the project you launched from produces a GREEN run in which your plugin was never loaded. Use `--all` whenever the plugin under test may not be enabled where you are standing, and read the skipping note before trusting a pass.

**Safety rail: never delete a claudx data root without an ordinary bootstrap pass
afterwards and a cooldown reset.** The shared-lib link it wrote repoints the
machine-wide `bootstrap_lib.pth`; deleting the root without relinking breaks every
plugin's import until the next unthrottled pass. Containment mechanics (what it
does and does not test) and the two other known escapes:
[docs/reference/testing.md](docs/reference/testing.md).

| Change touches ... | Default validator |
|---|---|
| skills / hooks / commands / engine code | `claudx` |
| `bootstrap.json` / manifest content | `claudx` (it reads manifests from disk) |

**`scripts/dev-tree.py` is the superseded path, and is not the one to reach for.** It repoints installPaths by rewriting the real `~/.claude/plugins/installed_plugins.json`, which is machine-global: every other session, running or subsequent, then sees the dev tree, and a crash before the restore leaves the machine that way silently. `claudx` covers both rows.

**Bypassable at your discretion.** This is a default, not a hard gate. Trivial changes -- a version-only bump, a doc/CLAUDE.md edit, a single-file mechanical fix -- don't need a smoke session; skip it and say so. An unambiguous publish go-signal does not silently waive validation, but you may explicitly bypass when the change can't plausibly break a runtime surface.

**A nested `claude -p` child under a scratch HOME/USERPROFILE is not logged in.** The macOS keychain lookup follows HOME, so a child launched that way exits with "Not logged in", and copying only the non-secret `oauthAccount` section of `~/.claude.json` does not fix it. The permission classifier refuses keychain workarounds -- symlinking `~/Library/Keychains` into the scratch HOME, a metadata-only `security find-generic-password` probe -- as credential access, even under owner authorization. What works: an owner-generated `claude setup-token` token passed only as the child's `CLAUDE_CODE_OAUTH_TOKEN` environment variable, never written to disk. A real HOME is not a safe substitute -- llm-scripting-kit's usage-verdict cache and the Codex sessions directory both derive from `Path.home()`, so a real HOME reads and writes real quota state and real session history regardless of other config variables. Worked example: docs/planning/quota-resilient-dispatch/drill-report.md (Layer B, 2026-09-24).

### Anti-pattern: creating a branch, or switching the one that is checked out

**Stay on `dev`. Do not create branches, and never run `git checkout` / `git switch`
to move the working tree onto another branch.** Work here happens in ONE shared
working tree that more than one agent session may be using at the same time, and the
checked-out branch is global to that tree. Moving it is not a local decision -- it
silently reaches into every other session running in this directory.

The failure is not that a branch is untidy; it is that **another session's commits
land on your branch instead of `dev`**, and it happens with no error and no warning.
The other session keeps working, runs `git commit`, and git faithfully commits to
whatever branch the tree is on. If your branch was cut from `master` (the natural
choice for a review or a cherry-pick), those commits are parented on `master` and
have silently lost every `dev` commit beneath them.

A worked incident where this stranded another session's commits:
[docs/reference/shared-tree-git-discipline.md](docs/reference/shared-tree-git-discipline.md).

**Scope a review or a diff with a range, never with a branch.** `git log`, `git diff`,
and `prepare_review.py` all take `<a>..<b>` / `<a>...<b>` and read history without
touching the tree. Path scoping (`-- plugins/foo/`) narrows further. If commits are
non-contiguous, review each one individually (`<sha>^..<sha>`) -- several small reviews
beat one branch switch. A review never needs a second checkout; see "Worktrees and
scratch copies" below for the narrow cases that do.

**Publishing does not need a branch either.** `publish.py` owns the `dev` -> `master`
flow. The one case that historically wanted a feature branch -- gotcha 1, cherry-picking
past unrelated `dev` commits -- is a decision to ship alone with `--only`, not to solve by
creating a branch yourself.

### Worktrees and scratch copies

The user's rule for this repo, and the reason worktrees are rare here:

1. **All work happens in the project folder.** Edits, tests, commits, reviews and
   publishes run in this checkout. Parallel writers share it, each owning separate
   files stated in its brief; a writer's own check covers only its files, and the full
   check runs at the join.
2. **A worktree is only ever a READ-ONLY copy of the project at a specific commit**,
   created when a task truly needs one (for example, running the suite at the merge-base
   to learn whether a failure predates your change). Nobody edits project files in it;
   tool caches such as `__pycache__` are fine. Work never moves into a worktree while
   this folder sits idle.
3. **A scratch directory may hold a SUBSET of the project** when a tool needs files in
   a state the folder does not hold (`publish.py` extracts the ~50 generator inputs this
   way). It holds tool inputs and outputs, never edits to project files.
4. **Whoever creates a worktree or scratch directory removes it as soon as it is no
   longer needed.** A script removes its own in a `finally`. This overrides the general
   habit of leaving scratch behind.

Before creating either, prefer the no-copy route: git reads any revision without a
checkout (`git show`, `git diff`, `git log`), git plumbing builds commits without one
(`publish.py`'s projection), and "did my change break this test?" is usually answered
by undoing your own edits for a moment or by reading the diff. The harness is set to
match in `.claude/settings.json` (`EnterWorktree` denied, `worktree.bgIsolation: none`).

### Committing and pushing to `dev` is unrestricted -- only PUBLISHES are gated

**Standing policy. Commit and push to `dev` freely, without asking.** `dev` is a working
branch: nothing on it reaches a consumer by your hand, because `master` is the cache
source. The only gated action is a PUBLISH (`dev` -> `master` via `publish.py`), which
needs the user's intent per the go-signal rule above.

**The implied contract: pushing to `dev` consents to someone else publishing your
work.** The gate is on the ACT of publishing, not on your commits; the next publish by
anyone carries whatever you pushed, whole-range, without asking you.

- **Push at consumer quality, not at working-branch quality.** If work must not ship
  yet, the mechanisms that hold it back are a `published: false` plugin or simply not
  pushing -- not delaying the push itself.
- **Work incrementally; do not hold files open across a long stretch.** Land small,
  shippable pieces and keep pushing rather than hoarding a large uncommitted pile.
- **Your work shipping is not evidence you shipped it.** Check CONTENT to know whether
  something published; a version can advance with no action from your session at all.

**As the publisher, ship what is pushed.** Given a go-signal, do not stop or ask
whether other sessions' commits in the range are ready -- pushing to `dev` was their
declaration that the work is publishable.

**Do not coordinate around other agent sessions.** The tree is shared; do not wait for
it to be clean, ask about someone else's uncommitted work, or treat unrelated commits
riding along on a push as a reason to stop or as your regression.

Two habits survive this policy, because they are hygiene rather than permission-seeking:

- **Scope the commit to your own files, by explicit path**, so `git log` stays
  readable and a revert stays surgical.
- **When the index already holds someone else's staged work, do not fight it.**
  `git commit -F <msg> -- <your paths>` commits exactly those paths without touching
  the index; reserve `git reset` for an index you own.

#### Anti-pattern: unstaging another session's work to scope your own commit

**Do not run `git reset`, `git restore --staged`, or `git rm --cached` on a file you did
not stage in order to scope your own commit.** The index is shared mutable state that
you do not own, and unlike the working tree it holds no history -- there is no reflog
for "what was staged." Undoing someone's staging destroys the only record of a decision
they made, and you cannot reliably put it back, because the index does not tell you WHY
a file was staged. A `git rm --cached` (a deliberate untrack) and an accidental
`git add` of a deleted file look identical in `git status`. Restoring one as the other
silently corrupts their commit.

The operative question is **does the index hold information that exists nowhere else?**
That is what makes the default so strict, and it also bounds it. A staged deletion or
untrack encodes an intent recorded in no file and no commit -- irrecoverable. A staged
snapshot of content that git history already contains encodes nothing new.

So there is one narrow exception, and every clause is mechanically checkable. You may
discard a staged state when ALL of these hold:

- `git diff HEAD` is empty for those paths -- no working tree, anywhere, holds the
  staged content; nobody is mid-edit on it.
- The staged content is strictly SUPERSEDED by HEAD, demonstrably: a version number
  that goes backwards, or text that is an older revision of what HEAD already carries.
- No path is staged as a deletion or an untrack.

Then the index is a stale re-add, its content is in history, and discarding it loses
nothing. Outside that conjunction, assume the index is load-bearing. When it is
load-bearing and merely inconvenient, you never needed to touch it -- see the correct
move below.

This is easy to walk into precisely BECAUSE the staging discipline above is right: you
`git add` your own paths, run the mandatory `git diff --staged`, and find files that are
not yours. The documented rule says the staged set must be exactly your files -- so
unstaging the rest feels like compliance. It is not. The rule exists so your COMMIT is
scoped; it was never a licence to edit a shared index.

Nothing about the situation ever requires touching another session's index. The correct
move, available from the start, is:

```bash
git commit -F <msg> -- <your paths>     # commits those paths; index untouched
```

Worked examples, including a case where the exception applied correctly:
[docs/reference/shared-tree-git-discipline.md](docs/reference/shared-tree-git-discipline.md).

If you have already unstaged something that was not yours, say so plainly rather than
quietly reconstructing it -- the other session can restate its intent in one line, and
you cannot read it out of the index. That disclosure is required even when the exception
above applied, because "the checks passed" is a claim the other session may want to
test.

### Safe-publish practices

Publishing is the riskiest moment in this repo because it broadcasts to every consumer. Two failure modes have happened, both recoverable but visible (the retraction commits in `git log master` are the scars). Avoid them with these checks. Rationale and worked recoveries for every gotcha below: [docs/reference/publish-reconcile.md](docs/reference/publish-reconcile.md) ("Publishing rationale").

**Gotcha 1: a release ships everything in the range, not just your feature.** `dev` typically contains in-flight work from other plugins. **Mandatory check before any publish:**

```bash
git fetch origin
git log --oneline $(uv run python scripts/publish.py --print-range-base)..dev
```

Use `publish.py --print-range-base` as the range base, NOT `origin/master..origin/dev`. Given a go-signal, ship the range -- other sessions' commits in it are not a reason to stop (see "The implied contract" above). A self-contained change that must ship alone uses `uv run python scripts/publish.py --only <plugin>`; mechanics: [docs/reference/publish-reconcile.md](docs/reference/publish-reconcile.md), "Partial release".

See "Anti-pattern: creating a branch" -- do not branch from master to route around this. `publish.py`'s fast-forward shortcut is refused outright while any dev-only plugin exists.

**Gotcha 2: `git add <file>` sweeps pre-existing working-tree modifications.** **Mandatory check before any publish commit:**

```bash
git diff --staged
```

Read every line and confirm the staged set is exactly your files, even after a targeted `git add` -- the index may already hold another session's staged work. If anything is unrelated, `git restore --staged <file>` and use `git add -p` (or `git stash` the WIP first).

**Gotcha 3: a botched publish burns the version number.** A burned version is never reused -- patch-bump *past* it (e.g. 0.11.0 broken -> not 0.11.1, jump to 0.12.0) so every consumer's cache invalidates cleanly.

**Gotcha 4: unauthorized publish.** Running `publish.py` without a go-signal is the defect; see "Committing and pushing to `dev` is unrestricted -- only PUBLISHES are gated" for the implied contract.

**Recovery: how to retract.** A bad publish on master is fixed forward, never with `push --force` to master. Push a follow-up commit that either (a) reverts the bad commit and patch-bumps the affected plugins past the burned version, or (b) re-implements correctly under a new version. Never rewrite master history -- other machines have already fetched it.

**Submit gate:** Verify every changed plugin is version-bumped since the last publish, each stated pyproject version matches plugin.json, and marketplace derived data matches the manifests.
Applies to:
- plugins/
- .claude-plugin/marketplace.json

For a staged Git change, run `scripts/pre-commit-version-check.sh`; its existing version checks read the Git index. For a Git commit range or a Perforce changelist, establish the same three facts from the reviewed diff and repository state. The index-scoped command is evidence only when the index is the reviewed change; an empty staged run is not evidence.

**Keep architecture docs current** -- when modifying bootstrap behavior, update the bootstrap skill references (`plugins/bootstrap/skills/bootstrap/references/`) to reflect the changes. These are the source of truth for how the system works.

**Anti-pattern: silent bootstrap operations.** Every bootstrap check must log its outcome -- `ok_entries` when passing (verbose-only), `action_entries` when remediating (always visible). Adding a check that creates files, clones repos, or writes config without emitting a log entry is a bug. See the "Every check must log its outcome" principle in [engine-internals.md](plugins/bootstrap/skills/bootstrap/references/engine-internals.md).

**`uv run [--extra dev] python` is for plugins-kit's own maintainer commands, run inside this checkout** -- never bare `python` or `python3`. This covers this repo's own `scripts/*.sh` and `scripts/*.py` entry points, and documented human commands (this file, CONTRIBUTING.md, `docs/**`). On Windows, the system PATH contains Microsoft Store stubs (`WindowsApps/python.exe`) that take precedence over any user PATH entry, causing bare `python`/`python3` to fail with "Permission denied" (exit 126) in Git Bash. On macOS, bare `python` often doesn't exist. Since bootstrap guarantees `uv` is available, `uv run python` is the standard way to invoke Python from a maintainer command in this project: it resolves the correct Python, syncs and activates THIS checkout's own venv (a `uv.lock` change is picked up immediately, not at the next bootstrap pass), and works on all platforms. A maintainer Python script spawning a same-environment child process uses `sys.executable`, not another `uv run python` (already true in `scripts/publish.py`).

**This does not extend to `plugins/**`.** Code that ships to a consumer --
a shipped plugin script, a hook, a manifest command, a skill example -- never
uses `uv run python`. Run from a foreign working directory it creates or
syncs a venv the caller never meant; see "Python interpreter variables"
below for what those call sites use instead.

**Scoped exception: Python CLI launcher shims.** `uv run python` resolves the
venv from the CWD, so a launcher a user invokes from any directory -- the four
Python-invoking `plugins/<name>/bin/` shims (hue-kit, job-kit,
llm-scripting-kit, secrets-kit) and their `.cmd` twins -- would pick up the
wrong environment, or none. Those shims resolve an absolute interpreter
instead: the deterministic bootstrap-provisioned standalone Python path
first; `BOOTSTRAP_PYTHON` next, but only when that deterministic file is
absent and the variable's own realpath resolves inside the standalone
directory (a stranger's `BOOTSTRAP_PYTHON` must never substitute for the file
the shim was written to find); then the plugin venv by its version-independent
`~/.claude/plugins/data/<marketplace>/<plugin>/.venv/` path. POSIX shims fall
back to `python3`, then `python`; `.cmd` shims fall back to `python.exe`. The
absolute-path preference avoids the Windows Store stub when the preferred
interpreter exists, but the PATH fallback can still resolve it. The
`bin/qwen3*-server` scripts are outside this exception -- they invoke
`model-server.sh`, not Python.

Outside these two exceptions, every other FORCED call site -- bootstrap or
recovery code that produces the interpreter itself, Claude Code hook scripts
bootstrap ships, `bootstrap-stuck-fix`, and other levers -- uses the same
deterministic-path-first chain: the deterministic standalone path, then
`BOOTSTRAP_PYTHON` only when that file is absent and the variable's realpath
resolves inside the standalone directory, then the existing PATH chain. Each
such call site must state its reason. Exceptions to this standard are
recorded in the repo guard's allowlist as `{path: (anchor_string, reason)}`,
where `anchor_string` is text that already exists at the call site; the
guard's staleness check asserts that text is still present. No new marker
comments are added to shipped files for this purpose.

## Python interpreter variables

Every other Python call site -- shipped plugin scripts, hooks, manifest
commands, skill examples, and any command documented for a CONSUMER project --
invokes Python through two environment variables bootstrap exports, never
bare `python`/`python3`/`py` and never `uv run python`:

    # Project code -- prefers the project's own venv, falls back to bootstrap's:
    "${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}}"

    # Bootstrap/plugin machinery and stdlib-only glue -- always the bootstrap interpreter:
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}"

Project calls DEFAULT to the project's own venv, then to the bootstrap
interpreter, and are never forced past that. Bootstrap's own code -- the
SessionStart hook, levers, hook scripts bootstrap ships -- is FORCED to run
under the same interpreter every time, using the deterministic-path-first
chain above instead of either variable form. The Debugging section's engine
invocation below is an instance of the forced case:
`"$BOOTSTRAP_PYTHON" plugins/bootstrap/engine/bootstrap_engine.py ...`, never
`uv run python` -- the engine is bootstrap's own code.

Contract, the full visibility table across every surface, per-shell
copy-paste forms, and the opt-outs: the `/bootstrap` fact `python_interpreter`
and `plugins/bootstrap/skills/bootstrap/references/python-interpreter.md`.
Guard: `tests/repo-scripts/test_python_invocation_standard.py`.

**Shell scripts must survive bash 3.2 and zsh.** `/bin/bash` on macOS is bash
3.2 (no bash 4+ since the licence change, and none at all without Homebrew),
and the macOS login shell is zsh. Two consequences bite hardest -- a
possibly-empty `"${ARR[@]}"` is a FATAL `set -u` error before bash 4.4 (write
`${ARR[@]+"${ARR[@]}"}` instead), and bash-only builtin flags such as `read -p`
fail under zsh (`printf %s "..."; read -r _` is the portable hold-open). Both
apply to this repo's own `scripts/*.sh` as much as to shipped plugin code, and
neither surfaces as a test failure. A Windows session cannot self-check these
-- verify on a Mac, or assert POSIX-only constructs in a test.

**Skill-based document placement** (package cohesion): when creating a document, ask "what skill does this belong to?" and place it by the CCP/CRP/ADP framework -- `plugins/skills-kit/skills/md-domain/references/cohesion-principles.md` is the SSOT for those principles and the placement algorithm. If no existing skill fits, create a stub skill and let the document live as a progressively-disclosed reference inside it.

**That rule applies to documents a SKILL's readers need. It stops at the plugin
boundary.** Everything under `plugins/<name>/` is copied into a consumer's plugin
cache, so placing maintainer-only material in a skill's `references/` publishes it
-- the OP-1 violation whose stated remedy is the opposite move, "to `docs/`,
`scripts/`, or a task folder" (`docs/reference/plugin-opinion-razor.md`). Repo
material -- this repo's git discipline, its test suite, its publish flow, its own
razor -- therefore belongs in `docs/`, and creating a stub skill to hold it would
be the defect, not the fix. The test is OP-1's: **who reads this on a machine that
is not ours?** Nobody -> `docs/`. A consumer of the skill -> that skill's
`references/`.

This boundary is stated because an md-domain audit will otherwise keep finding it.
The `ancestor_convention_conformance` criterion reads the paragraph above as an
ancestor-declared convention and flags every `docs/reference/*.md` as misplaced,
which is a correct reading of an incomplete rule.

**The plugin-opinion razor.** Every workflow opinion a plugin imposes is
configurable-with-a-default or registered as a deliberate stance. Criteria,
register and submit gate: `plugins/CLAUDE.md` and
[docs/reference/plugin-opinion-razor.md](docs/reference/plugin-opinion-razor.md).

**Instructions we ship to Claude must be checkable.** Text a plugin ships to
an agent must be true as written, name a file in version control backing any
authority it claims, and neither withhold from the user nor bypass them.
Criteria and submit gate: `plugins/CLAUDE.md` and
[docs/reference/agent-directive-standards.md](docs/reference/agent-directive-standards.md).

**A published plugin ships to other developers.** Before adding content to a
shipped plugin, ask **who reads this on a machine that is not ours** -- if the
answer is "nobody", it belongs in `docs/`, `scripts/` or a task folder.
**Plugin boundaries are hard boundaries for cohesion work** -- never relocate a
skill or reference across one to improve cohesion; surface it as an insight
instead. That rule, and reference-file design within a skill: `plugins/CLAUDE.md`.

## Plugin System

Plugins follow the Claude Code plugin spec:
- **Marketplace manifest** (`.claude-plugin/marketplace.json`): Lists available plugins with name, version, source path
- **Plugin manifest** (`.claude-plugin/plugin.json`): Per-plugin metadata (name, version, description, keywords)
- **Skill discovery**: Claude Code scans `skills/` directories for `SKILL.md` files
- **Variable expansion**: `${CLAUDE_PLUGIN_ROOT}` resolves to the plugin's install path at runtime

**Plugin dependencies on bootstrap.** Every plugin except bootstrap declares
`"dependencies": ["bootstrap"]`, enforced at pre-commit and again in
publish preflight. Mechanics and the bootstrap_guard runtime shim:
`plugins/CLAUDE.md`.

### Anti-pattern: hand-creating an artifact a plugin is supposed to produce

The sibling of [repairing a wedged machine by hand](#anti-pattern-repairing-a-wedged-machine-by-hand), one level up from bootstrap: that section is about not hand-fixing a *machine*, this one is about not hand-making an *output*. Same root error -- doing by hand the thing whose whole point was that the mechanism does it.

**When a plugin owns a workflow that produces an artifact -- a generated stub, an index, a config file, a report -- do not create that artifact yourself.** Build or fix the producing action, publish it, install it, and RUN it to make the artifact.

Producing it by hand proves nothing about the workflow, and the workflow was the deliverable. A hand-placed file cannot distinguish "the action works" from "the action is broken and I covered for it" -- and it looks like success in exactly the way that stops anyone looking further. The artifact existing was never the goal; the artifact existing *because the plugin made it* was.

The failure is tempting for an honest reason: the real path costs an edit, a version bump, a publish, a restart to pick it up, and only then a run. That round trip is the cost of shipping, not an obstacle to route around. Publishing is gated on the user (see "Committing and pushing to `dev` is unrestricted -- only PUBLISHES are gated"), so when the workflow is not yet published, **say so and wait** rather than substituting a hand-made result to keep moving.

If a *prerequisite* of the workflow is missing -- a source file it reads, a credential, an editor build -- report the missing prerequisite. Do not satisfy it by producing the final output directly; that discards the signal that the prerequisite was missing at all.

Corollary for verification, and the part most often skipped: **a plugin change is not verified by re-reading the diff.** It is verified by the published plugin doing the thing on a machine that installed it. Until then the correct status is "written", not "working" -- and a background agent's report of what it intended is not evidence either.

### Hook JSON Format & Plugin Cache Layout

The Claude Code hook-JSON output contract (exit-code semantics, universal fields, the per-event decision table, the `hookSpecificOutput` rule) and the on-disk plugin cache/registry layout (`~/.claude/plugins/` paths) are static CC platform reference -- see [docs/reference/claude-code-plugin-platform.md](docs/reference/claude-code-plugin-platform.md). Canonical upstream: https://code.claude.com/docs/en/hooks.

The one plugins-kit-specific wrinkle to keep in mind: bootstrap runs in a **background mode** -- the engine writes output to a pending file that the UserPromptSubmit hook re-emits as its own stdout, because Stop hooks don't support `hookSpecificOutput` (so UserPromptSubmit carries the `additionalContext` for Claude).

### Debugging

```bash
# Version report -- shows local, marketplace, installed, and cached versions for all plugins
bash scripts/plugin-versions.sh

# Run bootstrap engine in console mode (plain text, no JSON, no log writes)
# WARNING: --console is NOT read-only. It still fetches marketplaces, installs
# versions into the cache, and rewrites installed_plugins.json. Never point it at
# a wedged machine before snapshotting its state -- see the hand-repair
# anti-pattern in the Bootstrap section above.
# The engine is bootstrap's own code (forced form), never `uv run python` --
# see "Python interpreter variables" above.
"$BOOTSTRAP_PYTHON" plugins/bootstrap/engine/bootstrap_engine.py --plugin-root plugins/bootstrap --data-dir ~/.claude/plugins/data/plugins-kit/bootstrap --console

# Verbose mode (show ok/cached entries too)
"$BOOTSTRAP_PYTHON" plugins/bootstrap/engine/bootstrap_engine.py --plugin-root plugins/bootstrap --data-dir ~/.claude/plugins/data/plugins-kit/bootstrap --console --verbose
```

## Task folders live in a private tasks repo, linked in at `dev/tasks`

`.gitignore` carries `dev/tasks/`, and that entry is **load-bearing**: it is
what stops this repo's git from traversing the link and staging private task
content into a public repo. Do not remove it, and do not add a `!dev/tasks`
negation.

Link model, the task-CLI contract, and the 0.35.0 misreporting bug:
[docs/reference/task-folders.md](docs/reference/task-folders.md).

## Preferences

- **No temporal deixis in tracked documents.** Never "recent(ly)", "new", "just
  shipped", or similar now-relative phrasing in any tracked document, including
  agent-facing internal documents such as this `CLAUDE.md` -- state what a thing
  IS, cite a source, and use absolute dates when a date matters. Now-relative
  claims rot silently and are unverifiable.
- **ASCII only in tracked files.** Every tracked file in this repo -- source,
  manifests, skills, and the prose under `docs/` alike -- must be ASCII. Write
  `--` rather than an em-dash, `'` rather than a typographic quote, and `->`
  rather than an arrow.

  **Scoped exception: box-drawing characters in diagrams.** In any tracked file,
  the Unicode Box Drawing block (U+2500 to U+257F) is permitted where the
  characters are drawing a diagram -- a tree, a table frame, a flow. Do not flag
  those. The exception is content-kind-scoped and no wider: a box-drawing
  character used as ordinary punctuation is still a violation, and no other
  non-ASCII character is covered, including status glyphs such as a check mark
  or a ballot X. ASCII substitutes exist for every character this rule names,
  but not for line art -- replacing it mangles the drawing rather than
  transliterating it, which is why this one carve-out is worth its cost.

  The rule is declared HERE, and not only in a contributor's personal
  configuration, because an audit's ancestor-convention check walks this repo's
  CLAUDE.md chain: a convention stated only outside the repo binds nothing
  inside it, and non-ASCII then passes or fails according to who is auditing.
  Ancestor conventions are quoted verbatim by that check, so the wording above
  is the rule as applied, and the exception is honoured by the same mechanism.

  Files predating this rule are NOT swept. They surface one at a time through
  the normal review cycle, where each violation is judged in the change that
  touches it. Do not open a repo-wide transliteration pass: it would rewrite
  files that other sessions are editing, for a benefit the review cycle
  delivers anyway.
- **Never use the memory system** (`~/.claude/projects/*/memory/`). Always update `CLAUDE.md` instead -- it is machine-independent and checked into the repo, so all machines and sessions share the same context.

## Insights

```yaml
claude_md:
  _schema_version: "1"
  scope:
    directory: plugins-kit (root)
    covers:
      - dependency management posture across plugins
      - how to install or update plugin dependencies
      - bootstrap engine / hook invocation
      - cross-plugin observations no single plugin owns -- a cohesion opportunity
        spanning two plugins, or a refuted proposal to couple them
      - shared-tree git discipline
      - publish flow
      - test-suite discipline
      - Python invocation standard
    excludes:
      - per-plugin internals confined to ONE plugin that ship with it (covered
        by per-plugin CLAUDE.md / bootstrap.json); maintainer-only single-plugin
        facts stay here because plugins/<name>/ ships to consumers
  insights:
    - id: suite_green_is_host_dependent
      keywords: [full suite green, pre-existing failure, is this my regression, baseline, arm64, apple silicon, amd64, detect_arch, platform.machine, host arch, faked current_os, monkeypatch, worktree baseline, test isolation, passes on my machine]
      summary: "\"The full suite is green\" is a per-HOST claim, not a repo fact. Establish a baseline at the merge-base on YOUR machine before treating any failure as your regression -- and when mocking the platform, mock the ARCH too, not just the OS."
      detail: |
        Five tests/bootstrap/test_manifest_normalization.py scoop tests failed on every arm64
        machine and passed on every amd64 one, from their introduction (b14471bf) until
        2026-09-01. The mechanism generalizes past this one case and is the reusable part:
        _resolve_download_def keys on f"{current_os}-{detect_arch()}", and detect_arch() reads
        the REAL host CPU via platform.machine() no matter what current_os a test fakes. The
        fixtures use p4-kit's real "windows-amd64" key with current_os="windows" but never
        monkeypatched detect_arch, so on arm64 the composed key is "windows-arm64", nothing
        matches, and scoop promotion silently does not fire. The tests were not stale and the
        engine was not broken -- production always derives current_os and detect_arch() from
        the same real machine, which is exactly what lets a Windows-on-ARM box decline an
        amd64-only binary. The fix was to pin detect_arch in the five tests.
        TWO DURABLE RULES.
        (1) AUTHORING: a test that fakes one axis of the platform must fake every axis the
        code under test reads. Faking the OS while leaving the arch live produces a test that
        is green on the author's machine and red on half the fleet, and the failure looks like
        a defect in the feature rather than a gap in the mock.
        (2) DIAGNOSIS: never accept a tracked document's claim that the suite is green as
        evidence about YOUR machine. First undo your own edits for a moment and re-run the
        failing tests; when that cannot answer it, run the suite at the merge-base in a
        read-only worktree (never a branch switch -- the tree is shared; remove the worktree
        afterwards, per "Worktrees and scratch copies") and diff the failure sets. That costs a
        couple of minutes and is the only thing that distinguishes "I broke this" from "this
        was already red here". A standing green claim is the most misleading kind of stale
        documentation, because it converts someone else's pre-existing failure into your
        apparent regression.
      origin: "2026-09-01 -- a session investigating seven standing failures found five were an arm64 artifact and one a POSIX-vs-Windows path artifact; only one (awesome-kit missing an openai declaration) was a real gap. The root CLAUDE.md had asserted the suite was green, which is why a worktree baseline had to be built by hand to trust anything."
      added: "2026-09-01"
    - id: bootstrap_json_for_deps
      keywords: [bootstrap.json, plugin dependencies, venv, pyyaml, uv, no manual install, dependency manifest]
      summary: Plugin Python dependencies are declared in bootstrap.json + pyproject.toml and installed by the bootstrap engine using uv. Do not run pip / python -m venv manually.
      detail: |
        Each plugin that ships Python scripts declares its venv requirements in bootstrap.json
        ("venv": { "check_imports": [...] }) and its actual dependencies in pyproject.toml. The
        bootstrap engine creates a venv at ~/.claude/plugins/data/<marketplace>/<plugin>/.venv/
        using uv, installs deps from pyproject.toml, and verifies the check_imports succeed. Do
        not pip install at the user or system level; do not python -m venv manually. If a plugin
        needs a new dep, add it to that plugin's pyproject.toml and update check_imports in
        bootstrap.json.
      origin: User directive 2026-04-28 during YAML contract refactor; existing pattern in unreal-kit/bootstrap.json + p4-kit/bootstrap.json.
      added: "2026-04-28"
    - id: never_run_bootstrap_hook_directly
      keywords: [bootstrap hook, sessionstart, force update, plugin refresh, install update, session-bootstrap.sh, run hook directly, force a pass, anti-pattern, superseded]
      summary: "Do NOT hand-invoke session-bootstrap.sh or bootstrap_engine.py to force a pass on a WEDGED or under-diagnosis machine. Reset the cooldown and let the next real session run it. Post-update convergence on a HEALTHY machine (the bootstrap skill's `manual_convergence` fact: `bootstrap run`, which runs the SessionStart engine pass exempt from both skip gates) is the one sanctioned manual run."
      detail: |
        The former guidance told you to invoke plugins/bootstrap/hooks/sessionstart/session-bootstrap.sh
        directly to force a refresh. Treat that as retracted.
        See "Anti-pattern: repairing a wedged machine by hand".
      origin: "User directive 2026-07-27, superseding the 2026-04-28 directive. The original guidance was followed during a 'not cached' investigation and destroyed the failing state before it could be diagnosed."
      added: "2026-07-27"
    - id: bootstrap_cooldown_reset
      keywords: [cooldown, bootstrap not running, force bootstrap, plugin update not applying, last_run_epoch, bootstrap-reset-cooldown, silent skip, no bootstrap log]
      summary: Bootstrap throttles itself per-project via a cooldown file; clear it with bootstrap-reset-cooldown.sh when bootstrap appears to be ignoring you.
      detail: |
        Symptoms: a published update doesn't take effect, a bootstrap.json change isn't applied,
        or a plugin's bootstrap.log is stale. Reset with
        `bash plugins/bootstrap/scripts/bootstrap-reset-cooldown.sh` (`--all`, `--status`).
        The cooldown is the only run throttle and the right tool 99% of the time. Commands and
        guardrails: the "Bootstrap" section above; mechanics: the /bootstrap skill.
      origin: User directive 2026-05-05 -- documentation gap surfaced when a unreal-kit publish appeared not to apply.
      added: "2026-05-05"
    - id: cooldown_registry_invalidation
      keywords: [cooldown bypass, installed_plugins.json, known_marketplaces.json, stale shared_libs, version bump not applied, mtime, -nt, registry change, single-pass convergence, fewer reloads, reload-plugins vs restart, when to reload, when to restart, hooks need restart myth, SessionStart re-fire, script content live, registration reload, session-id guard, last_session_id, claude --resume, resume skips bootstrap, two skip gates, Layer 1, Layer 2]
      summary: BOTH session-bootstrap.sh skip gates -- the Layer-1 session-id guard AND the Layer-2 per-project cooldown -- auto-bypass when installed_plugins.json/known_marketplaces.json is newer than their stamp, so a plugin update re-arms a real pass on the next session (fresh OR `claude --resume`, which reuses the session_id) without a manual reset. Layered-bootstrap.json edits still need a manual reset.
      detail: |
        Both gates bypass when installed_plugins.json/known_marketplaces.json is newer (mtime)
        than their stamp; a skip never refreshes the stamp, so the bypass stays armed until a
        pass actually re-provisions. Manual bootstrap-reset-cooldown (clears BOTH gates) is
        needed only for a LAYERED bootstrap.json edit, which touches no registry file.
        See "Reload vs restart" in Development Workflow for the reload/restart rule.
        Full mechanics, the convergence sweep, the reload-nag (_reload_advice), and the
        probe method:
        plugins/bootstrap/skills/bootstrap/references/plugin-reload-lifecycle.md and the
        /bootstrap skill's update_lifecycle fact. Companion to bootstrap_cooldown_reset.
      origin: "Feedback report 2026-05-31 -- openrouter-kit 0.1.5 -> 0.2.0 publish left a consumer's _shared_libs stale because the cooldown blocked the resync across restarts. Implemented Part 1 (registry-change bypass) + Part 2 (convergence sweep)."
      added: "2026-05-31"
    - id: single_session_update_protocol
      keywords: [harvest, single-session update, engine_ran_version, harvest_launched_version, two restarts, bootstrap-display.sh, UserPromptSubmit, installPath, claude --resume, advise restart, provisioning done, update converged, anomaly, harvest no-op, script invocation, stamps.py]
      summary: Bootstrap converges its OWN version updates in ONE session via the "harvest" -- the UserPromptSubmit hook launches the already-fetched new engine in-session instead of waiting for a second restart. Provisioning is done when the engine_ran_version stamp == the installed version; a restart is then needed only to load new plugin CODE, not for provisioning.
      detail: |
        Advising the user: provisioning is complete the moment engine_ran_version == installed
        (harvest OR restart); the SessionStart "restart to load it" nag is moot once caught up --
        a restart then only reloads plugin CODE. Anomaly (surface, don't claim success):
        engine_ran_version staying BEHIND installed after a restart + a prompt means the new
        engine isn't running (check bootstrap.log + harvest_launched_version); installed never
        advancing means the fetch didn't happen. INHERENT caveat: a bootstrap-mechanism fix
        can't use that mechanism to adopt itself -- that one transition needs a manual
        bootstrap-reset-cooldown + an extra restart. Full operational guide (mechanism, state
        files, healthy flow, anomaly checklist):
        plugins/bootstrap/skills/bootstrap/references/plugin-reload-lifecycle.md and the
        /bootstrap skill's update_lifecycle fact.
      origin: "Built + hardened this session (2026-06-27): single-session protocol added (0.22.0), then live testing on this machine exposed two real bugs only live/script testing could catch -- the --resume session-guard skip (fixed 0.24.0) and the harvest's script-path import failure that meant it had NEVER fired in production (fixed 0.25.0). Verified end-to-end converging 0.26.0 hands-off."
      added: "2026-06-27"
    - id: registry_v2_empty
      keywords: [installed_plugins.json, empty registry, registry v2, "plugins {}", fresh machine, deleted plugins dir, provisions nothing, rescue, sessionstart missed, sessionstart-rescue, cache fallback, discover_cache_plugins, enabledPlugins, index.html empty, dev-tree synthesize, harvest blind, new machine test]
      summary: Claude Code registry v2 keeps installed_plugins.json PERMANENTLY EMPTY ({"version":2,"plugins":{}}) for marketplace installs -- enablement lives in settings enabledPlugins, code in the cache layout. Everything that read the registry needed a cache-scan fallback (bootstrap 0.47.0) and a SessionStart that races the fresh-machine plugin sync is caught by the UserPromptSubmit rescue (0.46.0).
      detail: |
        Engine-side fixes (rescue 0.46.0, cache-scan fallback 0.47.0) are owned by the bootstrap
        skill references (engine-internals.md, plugin-reload-lifecycle.md) -- consult those for
        mechanics. The REPO-specific residue to remember here:
        - dev-tree.py must SYNTHESIZE entries for repo plugins the registry doesn't record,
          or dev-tree mode loads nothing on a v2 machine (claudx's own synthetic registry
          does the same job for the same reason). publish.py does not use
          dev-tree.py: its index.html regen passes generate.py a synthetic `--registry` built
          from the repo's own plugin.json files (the 0.47.0 release shipped an empty
          index.html when the page was built from the machine registry).
        - awesome-kit's generate.py needed the same cache fallback (awesome-kit 0.10.0,
          merge_cache_fallback) or the poster renders empty.
        - Claude Code still WRITES enabledPlugins to the live ~/.claude/settings.json on
          `claude plugin install` -- check `git diff` before assuming settings.json's committed
          state matches the live file.
      origin: "Live fresh-machine testing 2026-07-16 (this machine, wiped ~/.claude/plugins repeatedly); fixed in bootstrap 0.46.0 (rescue) + 0.47.0 (cache fallback) + dev-tree.py synthesis."
      added: "2026-07-16"
    - id: host_python_via_plugin_venv
      keywords: [host-side python, plugin venv, uv run python, ModuleNotFoundError, foreign cwd, project root, pyyaml, skill examples]
      summary: SKILL.md examples that invoke host-side Python never use `uv run python`, which resolves the venv from the cwd. The default form launches the script under `"${BOOTSTRAP_PYTHON:?...}"` and lets it re-exec into its plugin venv; the explicit plugin-venv path below remains valid for a script without a re-exec guard.
      detail: |
        Explicit plugin-venv interpreter, for a script without a re-exec guard
        (version-independent, resolves from any cwd):
          Windows:     ~/.claude/plugins/data/<marketplace>/<plugin>/.venv/Scripts/python.exe
          macOS/Linux: ~/.claude/plugins/data/<marketplace>/<plugin>/.venv/bin/python
        Variable forms: "Python interpreter variables" above. Re-exec rule:
        plugins/bootstrap/skills/bootstrap/references/python-interpreter.md.
      origin: "Surfaced 2026-05-05 in unreal-kit fix-up-redirectors -- broke Phase 2 with ModuleNotFoundError: yaml. Fixed in 0.9.4."
      added: "2026-05-05"
    - id: manifest_changes_need_version_bump
      keywords: [bootstrap.json, manifest change, version bump, cache key, silent divergence, download recipe, dead config, install path, installPath]
      summary: Edits to bootstrap.json (or any per-plugin manifest) need a version bump to reach consumers, same rule as code changes -- the engine reads each plugin's bootstrap.json from its cached installPath.
      detail: |
        The bootstrap engine's per-plugin loop reads `bootstrap.json` from the plugin's
        `installPath` recorded in `~/.claude/plugins/installed_plugins.json`. That installPath
        is the cache directory (`~/.claude/plugins/cache/<mkt>/<plugin>/<version>/`), keyed on
        version. Adding a new tool, a `download:` block, a new venv import, etc. to bootstrap.json
        without bumping the plugin version means consumers still see the OLD bootstrap.json
        from their cache. The new manifest content is structurally invisible until a version
        bump triggers a cache refresh. Same "burned version" failure mode as code changes
        (CLAUDE.md gotcha 3). Surfaced when the tool-resolution redesign added jq's download
        recipe to bootstrap.json on dev without bumping bootstrap's version -- master and dev
        both showed v0.10.14 with completely different bootstrap.json content. Recovery: bump
        to a fresh version (e.g. 0.10.14 -> 0.11.0) and republish.
      origin: "Surfaced 2026-05-27 while smoke-testing the tool-resolution redesign via claudx (--plugin-dir all dev plugins). jq/gh never got download-recorded because the engine was reading the cached 0.10.14 bootstrap.json which had no download: block."
      added: "2026-05-27"
    - id: plugin_dir_doesnt_test_cross_plugin
      keywords: [--plugin-dir, claudx, claude-plugin-test, claude_plugin_test.py, smoke test, cross-plugin, bootstrap testing, installPath, dev tree, cache, layered manifests, synthetic registry, CLAUDE_BOOTSTRAP_DATA_ROOT, dev-tree superseded]
      summary: "BARE --plugin-dir still cannot exercise manifest content -- the engine's per-plugin loop reads each plugin's bootstrap.json from its cached installPath. The REMEDY changed: claudx runs scripts/claude_plugin_test.py, which closes the gap with a synthetic dev-layout registry plus a redirected data root, so it validates manifests without touching machine-global state."
      detail: |
        THE UNDERLYING FACT IS UNCHANGED. `--plugin-dir <dev tree>` only overrides Claude
        Code's loading of that plugin's hooks/skills. The bootstrap engine's per-plugin loop
        iterates `installed_plugins.json` and reads each plugin's bootstrap.json from its
        cached installPath, so a BARE --plugin-dir session exercises dev engine CODE against
        PUBLISHED manifests.
        Remedy: claudx; see "Pre-publish validation (default)".
      origin: Surfaced 2026-05-27 -- the claudx smoke test couldn't validate jq's new download recipe because the engine kept reading the cached bootstrap.json.
      added: "2026-05-27"
      updated: "2026-09-20"
    - id: code_review_cross_plugin_cohesion
      keywords: [code-review domain, git-code-review, p4-code-review, cross-plugin cohesion, bootstrap_lib.code_review, dec_13, domain not built, inter-plugin opportunity, surface not merge]
      summary: git-kit:git-code-review + p4-kit:p4-code-review are dec_13-justified doer-skills sharing one subject, but they are deliberately NOT merged into a domain -- the members live in different plugins, and plugin boundaries are hard boundaries for cohesion work. Recorded as an inter-plugin cohesion observation, not acted on.
      detail: |
        Both are technique-skills running the same pre-submit multi-agent review pipeline
        (identical reviewer roster, profiles, validators, submit-gate format); they differ only
        in VCS front-half (git ranges/auto-detect vs p4 changelist/shelving). The dec_13 merge
        criterion (2+ doers sharing a subject) is satisfied, and the VCS-neutral back-half
        (chunking + CLAUDE.md collection + submit-gate parsing) is ALREADY shared via
        plugins/bootstrap/bootstrap_lib/code_review/ (chunking.py + claude_mds.py). So the old
        "needs a shared abstraction first" blocker is gone. They are still NOT merged because:
        (1) the members are in separate plugins (git-kit, p4-kit) and a domain router cannot
        span plugins without relocating a member or spawning a new home plugin -- both barred by
        "Plugin boundaries are hard boundaries for cohesion work" in `plugins/CLAUDE.md`; (2) routing value is low
        -- git-vs-p4 is unambiguous from the workspace, so a natural-language front door adds
        little over the two already-auto-triggering skills. Correct cross-plugin sharing is the
        library both depend on (bootstrap_lib.code_review), which already exists. Do not
        re-investigate a code-review domain; the answer is "surface, don't merge."
      origin: Surfaced 2026-05-31 during the cohesion refactor -- after W2-proper, an Explore feasibility sweep found the shared lib already exists; user ruled cross-plugin relocation/new-plugin out of bounds for cohesion work.
      added: "2026-05-31"
    - id: orchestrate_yaml_capabilities_are_not_a_duplicate
      keywords: [orchestration.yaml, capabilities block, codex capabilities, adapter advertisement, CODEX_CAPABILITIES, guidance-migration, retire the yaml block, duplicate source of truth, xhigh, CODEX_EFFORT_MENU, display contract, direct dispatch vs completion seam, do not build, refuted]
      summary: orchestration.yaml's per-backend `capabilities:` block is NOT a stale copy of llm-scripting-kit's adapter advertisement -- they describe different code paths answering to different validators. Deriving the rendered codex effort menu from the advertisement would ERASE xhigh. Do not re-propose retiring the block.
      detail: |
        Operative: do not re-propose merging orchestration.yaml's capabilities block into
        llm-scripting-kit's adapter advertisement as a duplicate. Details:
        docs/reference/orchestrate-codex-boundaries.md.
      origin: "2026-09-01 -- the guidance-migration item of the llm-invoke task was investigated (codex/sol, adversarial brief) and returned DO-NOT-BUILD; the decisive claims were re-verified by hand against harness_adapters.py and adapter_capabilities.py. No code changed."
      added: "2026-09-01"
    - id: never_hand_repair_a_wedge
      keywords: [hand repair, manual fix, wedged machine, not cached, snapshot first, evidence destroyed, converges nobody, console not read-only, anti-pattern, ship the repair, diagnostic of last resort]
      summary: Never unwedge a machine by hand. A wedge is a specification for a repair that ships -- hand-fixing it converges nobody and destroys the only evidence of the defect. Snapshot the state first, then write the repair into bootstrap or bootstrap-stuck-fix.
      detail: |
        See "Anti-pattern: repairing a wedged machine by hand" in the Bootstrap section above.
      origin: "User directive 2026-07-27 after nine plugins reported 'not cached' and the engine was run by hand to clear it -- the machine recovered, the root cause became unrecoverable, and no fix shipped to any other machine."
      added: "2026-07-27"
    - id: a_check_must_be_shown_to_fail
      keywords: [test passes with the fix reverted, tautological test, vacuous test, revert-check, does this test anything, green for the wrong reason, prove the test fails, counterfactual, which test would fail, runtime assertion not enough, derive_short]
      summary: A test written alongside a fix can assert something that was already true. Revert the fix and watch the named test go RED before believing it -- a green run is not evidence the test exercises the fix.
      detail: |
        Three vacuous tests shipped in one task before the pattern was named
        (bootstrap-display-rule5, 2026-09-08): two called `_append_detail` directly with
        hand-written strings, asserting only that it honours an explicit `display=` label
        (pinned elsewhere, and silent about the production call sites the fix changed);
        one precedence test used a fixture identifier the claim glob `**/*.md` never
        matches, so the claim it was meant to outrank was never made.
        The live demonstration is the part that generalizes: reverting a production
        display site to a bare append left the RUNTIME test green, because `derive_short`
        cut the log line at a separator that happened to sit before the path. The rendered
        text was clean for a reason unrelated to the fix; only the AST guard over the
        source went red. So a passing runtime assertion is not evidence about the source.
        The counterfactual is the only reliable signal, and it is cheap. This is also why
        the task-level communication protocol asks which test would FAIL if the fix were
        reverted -- naming it forces the check to be run rather than assumed.
        Worked examples and the second shape: docs/reference/vacuous-checks.md.
      origin: "2026-09-08 -- bootstrap-display-rule5 shipped three tests that passed with their fix reverted; the third was caught only when a reviewer asked what the fixture actually claimed."
      added: "2026-09-08"
    - id: guard_cannot_see_its_own_subject
      keywords: [drift guard, byte-identity check, generated artifact, compare A to B, both move together, regenerate and it stays green, guard stops guarding, assert the property, detector on the rendered bytes, consistency check, test_skill_drift, banner]
      summary: A guard comparing a generated artifact to its generator is green whenever the two agree, so it cannot protect any property that regeneration would remove from BOTH sides. Assert such a property directly against the real detector.
      detail: |
        `tests/bootstrap/code_review/test_skill_drift.py` asserts the ten rendered
        code-review skill files are byte-identical to what `gen_code_review_skills.py`
        renders. That stops the two kits drifting and stops a hand-edit. It does NOT
        protect the machine-emitted banner: dropping `BANNER` from the template and
        regenerating leaves both sides matching and the check green, while the property
        that made those files safe to exclude from review is gone.
        The remedy is to assert the PROPERTY against the real detectors rather than the
        artifact against its source -- `detect_machine_emitted` on every rendered path,
        and `detect_signature_bytes` on the rendered BYTES, because the pre-commit guard
        reads blobs as bytes and a banner only the text detector finds would exempt a path
        with nothing to exempt.
        The general test, applicable to any A-vs-B check: ask what happens when A and B
        move together. If the answer is "it stays green", it is a consistency check and
        something else must carry the property. Companion to a_check_must_be_shown_to_fail;
        both shapes: docs/reference/vacuous-checks.md.
      origin: "2026-09-08 -- found while shipping the generated-skills machine-emitted exclusion in bootstrap-display-rule5; the drift guard would have gone on passing with the banner removed."
      added: "2026-09-08"
    - id: never_hand_make_a_plugins_output
      keywords: [hand-create artifact, hand-place file, copy the file myself, plugin should generate it, refresh action, generated stub, index, report, prove the workflow, publish and run, skip the round trip, missing prerequisite, written not working, verify by running]
      summary: Never hand-create an artifact a plugin's workflow is supposed to produce. Build or fix the producing action, publish it, install it, and run it -- a hand-placed file cannot distinguish a working workflow from a broken one.
      detail: |
        Hand-making a plugin-owned output proves nothing about its workflow and can hide a
        broken action or missing prerequisite; verify the published plugin by running it, and
        treat the result as "written", not "working", until it does so.
        Full narrative: "Anti-pattern: hand-creating an artifact a plugin is supposed to
        produce" in the Plugin System section above.
      origin: "User directive 2026-08-08 -- on a task that moved a generated artifact onto the durable-project-data pattern, the user directed that the artifact be created by the published refresh workflow rather than copied into place by hand."
      added: "2026-08-08"
    - id: plugin_opinion_razor
      keywords: [workflow opinion, configurable with a default, opinionated stance, register, hardcoded assumption, branch name, durability roots, reviewer roster, cooldown constant, treat as noise, deliberate deviation, audit, OP-1, seam]
      summary: Every workflow opinion a plugin imposes must be configurable with a sensible default OR registered in CLAUDE.md as a deliberate stance. Anything else is a finding, and the remediation is a config seam -- never prose telling the reader to tolerate it.
      detail: |
        A workflow opinion is an assumption about how the CONSUMER's team works (branch
        names, layout, VCS, must-be-committed, review rosters, thresholds, cadences), as
        opposed to something intrinsic to the plugin's job. Test: would a competent team
        reasonably do this differently and still want the plugin?
        The register exists to make branch (b) falsifiable. An opinion that is neither
        configurable nor listed in the register is a finding BY CONSTRUCTION, so "it is a
        deliberate stance" cannot be claimed at review time about an unregistered opinion.
        Sharpest signal, and it is greppable: when THIS repo cannot live with one of its
        own plugins' opinions, the seam was needed. The worked case is awesome-kit:task --
        it held that dev/tasks/<stub> is durable and must be committed, this repo
        gitignores dev/ entirely, and the remediation first chosen was root CLAUDE.md
        telling the reader to "treat those as noise". That fixed one machine, converged
        nobody, and left every consumer the same friction with no instructions. Grep for
        "treat .* as noise", "does not apply to this repo", "deliberate deviation".
        RESOLVED in awesome-kit 0.26.0, and the resolution sharpens the razor: the fix was
        NOT a config key. The plugin now DETECTS what git actually does instead of
        assuming the consumer's answer. Prefer
        that shape whenever the environment can be asked: a seam makes the user restate
        something the tool could have observed, and it is a second source of truth that can
        disagree with reality. Reach for a config key when the choice is a genuine
        PREFERENCE, not when it is an observable FACT. Note also what the prose remedy
        cost while it stood: the unhandled case was not merely noisy, it CRASHED archive
        mid-write, and the "noise" framing is why that read as expected friction for weeks.
        Full razor narrative: `plugins/CLAUDE.md`. Criteria OP-1..OP-7, detection methods,
        examples both ways, the table of known unremediated findings, and the audit
        procedure: `docs/reference/plugin-opinion-razor.md`. OP-1 (no maintainer-only material on the
        published surface) is PARTLY reachable by md-domain: skill-standards.md SR-4
        (reader fit) flags maintainer-only material inside a skill's references/*.md,
        which those claims used to carve out. It is one judgment criterion over one
        markdown document, not the razor -- OP-1 also covers non-markdown artifacts
        and surfaces outside a skills tree, which no lane reads.
      origin: "2026-08-08 -- user direction after the orchestrate skill was found shipping plugins-kit build machinery; generalized from that instance into a razor with a register, on the expectation that the repo will be audited against it."
      added: "2026-08-08"
    - id: no_build_machinery_in_published_plugins
      keywords: [published plugin, ships to consumers, other developers, build artifact, fingerprint, baseline, design doc, generator plumbing, maintainer only, who reads this off our machine, plugin cache, accretion, colocation]
      summary: Everything under plugins/<name>/ is copied to a consumer's plugin cache. Keep plugins-kit build machinery out -- ask "who reads this on a machine that is not ours", and if the answer is nobody, it belongs in docs/, scripts/, or a task folder.
      detail: |
        Three shapes to refuse in a shipped plugin: a GENERATED baseline or fingerprint
        (its header typically tells the reader to regenerate it with a scripts/ tool that
        does not exist in their install -- actively misleading); a DESIGN doc recording
        derivation rounds, drift checks and remaining work (our development history, not
        guidance); and BUILD PLUMBING embedded in a reference a consumer reads for
        guidance, which is the hardest to see because the file legitimately ships.
        The failure is incremental, not a decision: maintainer material rarely arrives as
        a new file, it accretes inside an existing shipped reference, so the file can
        double in size without anyone choosing to publish the additions. Be especially
        alert when a build step colocates its inputs with the artifact for convenience --
        that colocation IS a publishing decision, and the reason for it (e.g. a guard that
        polices one directory) usually outlives its own justification.
        md-domain reaches PART of this: a skill's references/*.md is claimed and audited
        by the audit_skill lane, whose SR-4 (reader fit) criterion flags maintainer-only
        material (skill-standards.md section 10). It is one judgment criterion over one
        MARKDOWN document. It cannot see a non-markdown artifact at all -- the
        decision-fingerprint.txt that motivated this insight is invisible to every lane --
        and it reads nothing outside a skills tree. Do not treat a clean audit as
        discharging this.
      origin: "2026-08-08 -- user pointed out that awesome-kit is published and used by other developers while the orchestrate skill was shipping decision-fingerprint.txt, orchestrate-2.0-design.md, and a tier-principles.md that compile-principles step 1 had grown from 642 to 1,065 lines with generator emits blocks."
      added: "2026-08-08"
    - id: never_unstage_another_sessions_work
      keywords: [git reset, git restore --staged, git rm --cached, unstage, shared index, scope my commit, staged set must be exactly my files, another session, concurrent staging, git commit -- paths, index has no reflog, narrow exception, stale re-add, superseded by HEAD]
      summary: Never unstage a file you did not stage, outside one narrow mechanically-checkable exception. Use `git commit -F <msg> -- <your paths>` to scope a commit without touching a shared index -- the index has no history, so undoing someone's staging destroys the only record of their decision.
      detail: |
        See "Anti-pattern: unstaging another session's work to scope your own commit" in
        Development Workflow; the second worked example is in
        `docs/reference/shared-tree-git-discipline.md`.
      origin: "2026-08-08 -- a unreal-kit stub staged as 588,614 deletions was `git restore --staged`-ed to scope an orchestrate commit; it was a deliberate `git rm --cached` paired with a staged .gitignore change, and putting it back required inferring that intent by hand."
      added: "2026-08-08"
    - id: never_create_or_switch_branches
      keywords: [branch, git checkout, git switch, feature branch, create a branch, scope a review, cherry-pick branch, shared working tree, concurrent session, stranded commits, worktree, stay on dev]
      summary: Stay on dev -- never create a branch or move the checked-out branch. The working tree is shared with concurrent agent sessions, so a branch switch silently redirects THEIR commits onto your branch.
      detail: |
        See "Anti-pattern: creating a branch, or switching the one that is checked out" in
        Development Workflow; the worked example is in `docs/reference/shared-tree-git-discipline.md`.
      origin: "2026-08-08 -- a review-bootstrap-cli branch was created off origin/master to scope a code review; a concurrent session then committed twice onto it, stranding both commits on a master-based throwaway branch that git branch --contains showed existed nowhere else."
      added: "2026-08-08"
    - id: orchestrate_routing_deferred_evidence
      keywords: [routing, unmeasured assumption, deferred evidence, evidence gaps, orchestration.yaml, tier-principles, orchestrate-2.0, benchmark, pool consumption, fan-out]
      summary: The routing policy in orchestration.yaml rests on seven UNMEASURED assumptions, recorded as a dated ledger in docs/planning/orchestrate/deferred-evidence-experiments.md -- read it before changing a routing row on the strength of an assumption it lists.
      detail: |
        The ledger names each gap (codex capability benchmarks, fable pool
        consumption, multi_agent_v2 scope, usage telemetry, agent-type
        effectiveness, the P0.3 volume threshold, Claude-side fan-out) and the
        experiment that would close it. It replaced section 7 of the deleted
        tier-principles.md (commit 4cb4d96c) and is maintainer material, which
        is why it lives under docs/planning/ rather than in the shipped skill.
        orchestration.yaml is hand-written config.
      origin: "2026-09-04 -- task orchestrate-2.0 could not be retired because its declared durable output had been deleted with the generated policy; the ledger was reconstructed into docs/planning/."
      added: "2026-09-04"
    - id: codex_dispatch_is_silent_on_failure
      keywords: [codex, codex exec, sandbox, workspace-write, windows.sandbox, absolute -C, add-dir, exit 0, no approval channel, permission spam, danger-full-access, reads unrestricted, bootstrap_lib.codex, CodexCliBackend, run_cli_streaming, discovery vs execution, does orchestrate use llm-scripting-kit, who invokes codex, two paths to codex]
      summary: Every way a codex dispatch can be misconfigured fails SILENTLY at exit 0, so codex machinery is built to refuse bad input rather than trust it. There are TWO paths to codex -- the completion seam (CodexCliBackend) and orchestrate rendering a `codex exec` argv for the agent to run -- and orchestrate does not call the seam, but it DOES reach bootstrap_lib.codex through llm-scripting-kit's CodexAdapter.
      detail: |
        Operative: judge a codex dispatch by its `-o` output file, not its exit status.
        Details: docs/reference/orchestrate-codex-boundaries.md.
      origin: "2026-08-10 -- empirical probing of codex-cli 0.146.0 while adding codex as a work backend; the shipped policy had hardcoded network-on, no windows.sandbox, and framed the missing approval flag as a mere gotcha."
      added: "2026-08-10"
    - id: stale_editable_self_install
      keywords: [editable install, __editable__ pth, venv runs old code, stale pth, plugin version change, silently old release, site-packages, venv_check, scan_editable_installs, own package not shared lib, fixed, detected and remediated]
      summary: RESOLVED. A plugin venv's editable self-install .pth used to keep pointing at the previous version's cache dir, so a plugin's OWN venv could silently execute a superseded release. bootstrap's venv_check now detects it on the recorded path and re-syncs; the durable lesson is the detection ARGUMENT, not an open defect.
      detail: |
        When the symptom is the right answer from the wrong source, test the wiring, not
        the behaviour. Never hand-clear a stale .pth. Fixed:
        venv_check.scan_editable_installs, pinned by tests/bootstrap/test_venv_check.py.
      origin: "2026-08-10 -- found while verifying that a published bootstrap_lib.codex resolved from the INSTALLED copy; llm-scripting-kit's own venv was resolving its superseded 0.6.1 cache dir. (A first reading blamed a second repo clone, because the recorded path spelled ~/.claude as D:\\Dev\\claude-settings; that is the same directory through the symlink, so compare paths with realpath before concluding the root moved.) RESOLVED 2026-08-21: two live bootstrap passes (bootstrap 0.86.0 -> 0.86.1, content-pipeline-kit 0.12.0 -> 0.13.0) each named the stale .pth and re-synced, both resulting .pth files were confirmed to point at the current version, and venv_check plus its tests were read to confirm both shapes are covered rather than only the two that happened to fire."
      added: "2026-08-10"
      updated: "2026-08-21"
    - id: bootstrap_python_interpreter_variables
      keywords: [BOOTSTRAP_PYTHON, BOOTSTRAP_PROJECT_PYTHON, python, python3, py, interpreter, bare python, uv run python, project_python, interpreter_env, python_interpreter, requires_bootstrap, python not found, default vs forced]
      summary: Every Python call site outside plugins-kit's own maintainer commands invokes Python through `BOOTSTRAP_PYTHON` / `BOOTSTRAP_PROJECT_PYTHON`, never bare `python`/`python3`/`py` and never `uv run python` -- the engine exports both every pass, the SessionStart hook writes both into every Claude session before any skip gate, and shell integration keeps `BOOTSTRAP_PROJECT_PYTHON` current per directory in a terminal.
      detail: |
        Two names, two call-site forms. Project code (documented human commands
        in a consumer project, a project's own scripts) uses the nested,
        defaulting form:
          "${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}}"
        Bootstrap's own code and stdlib-only glue use the forced form:
          "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}"
        DEFAULT VS FORCED: project calls default to the project's own venv, then
        to the bootstrap interpreter, and are never forced past that; bootstrap's
        own code is forced to run under the same interpreter every time.
        Full contract, the visibility table across every surface, per-shell
        forms, and the opt-outs: the `/bootstrap` fact `python_interpreter` and
        plugins/bootstrap/skills/bootstrap/references/python-interpreter.md.
        Guard: tests/repo-scripts/test_python_invocation_standard.py.
      gotchas:
        - "`uv run python` is a different mechanism (an interpreter choice made
          by the `uv` package manager) and is not a substitute for either
          variable in shipped code -- see the `uv run [--extra dev] python`
          paragraph above for where it still applies to plugins-kit's own
          maintainer commands."
        - A Bash tool call that `cd`s to a different project inside one Claude
          session keeps the session's starting values; they are not
          re-resolved per directory change inside a single session.
      origin: "2026-09-16 -- BOOTSTRAP_PYTHON / BOOTSTRAP_PROJECT_PYTHON shipped for every Python call site, on every OS, superseding an engine-process-only draft that never landed on this file."
      added: "2026-09-16"
  conventions:
    - rule: Commit and push to dev freely without asking; only a PUBLISH (dev -> master) needs the user. Do not coordinate around other agent sessions' concurrent work.
      keywords: [commit freely, push freely, no permission, dev branch, only publishes gated, other agents, concurrent sessions, shared tree, git commit -- paths]
      why: >-
        A commit or push is reversible working-branch state and a publish broadcasts to
        every machine, so only the publish is gated. Note the implied contract, stated in
        that section -- pushing to dev consents to ANOTHER session's next publish carrying
        your work, since a publish ships the whole range. Scope commits by path for
        readability, and use `git commit -F <msg> -- <paths>` when the index holds another
        session's staged work. See "Committing and pushing to dev is unrestricted" in
        Development Workflow.
    - rule: Stay on dev -- never create a branch or run git checkout/switch in this working tree; scope reviews with a commit range.
      keywords: [branch, git checkout, git switch, shared working tree, concurrent session, scope a review, range]
      why: The tree is shared with other agent sessions and the checked-out branch is global to it, so a switch silently redirects their commits onto your branch. See the never_create_or_switch_branches insight and the anti-pattern section in Development Workflow.
    - rule: All work happens in the project folder. A worktree is only a read-only copy of the project at a specific commit, a scratch directory only holds a subset of the project for a tool, and whoever creates either removes it as soon as it is no longer needed.
      keywords: [worktree, git worktree add, EnterWorktree, isolation, second checkout, scratch directory, parallel writers, read-only snapshot, cleanup, merge-base baseline]
      why: The user's standing rule for this repo. Parallel writers share this folder with separate file ownership, and git reads or builds any revision without a checkout, so a second copy is rarely needed and never a place to do work. See "Worktrees and scratch copies" in Development Workflow.
    - rule: When a machine is wedged, snapshot its state before any repair, and ship the repair in bootstrap or bootstrap-stuck-fix rather than fixing the machine by hand.
      keywords: [wedged machine, snapshot first, hand repair, manual fix, anti-pattern, ship the repair]
      why: A hand-repair reaches one machine and destroys the evidence every other machine's fix depends on. See the never_hand_repair_a_wedge insight and the anti-pattern section in Bootstrap.
    - rule: When adding a new plugin Python dependency, update <plugin>/pyproject.toml AND <plugin>/bootstrap.json venv.check_imports together.
      keywords: [pyproject.toml, bootstrap.json, dependency, venv, check_imports]
      why: pyproject.toml drives the actual install (via uv); check_imports tells the bootstrap engine what to verify post-install. Skipping check_imports leads to silent install failures.
    - rule: Never invoke pip, python -m venv, or any other Python package manager manually for plugin dependencies.
      keywords: [no manual install, pip, venv, plugin deps, bootstrap-only]
      why: Plugin dependency installs go through the bootstrap engine so they end up in the right per-plugin venv at ~/.claude/plugins/data/<marketplace>/<plugin>/.venv/. Manual installs land in the wrong location and confuse the engine's cache.
    - rule: Always run /git-code-review on non-trivial changelists before committing.
      keywords: [git-code-review, code review, pre-commit, non-trivial CL, multi-file commit, before submit, multi-agent review]
      why: Multi-agent review catches bugs and CLAUDE.md violations the author may miss; running it before commit lets the author fix issues in the same staging cycle rather than after the fact. "Non-trivial" = anything beyond a single-file mechanical change (typo fix, version bump). When in doubt, run it.
```
