# CLAUDE.md

## Project Overview

**plugins-kit** is the **development repository** (source of truth) for the plugins-kit Claude Code marketplace. It contains the source code for all plugins in the marketplace. Currently ships (published): **awesome-kit** (plugin-ecosystem poster, /html-pdf, task tracking), **bootstrap** (dependency management), **bootstrap-stuck-fix** (temporary remediation shim for a wedged bootstrap registry record), **cache-kit** (cache-usage reporting from transcripts), **claude-ui-kit** (status line + /statusline), **content-pipeline-kit** (library + skills for LLM-in-the-loop batch content pipelines), **git-kit** (Git/GitHub multi-agent code review + gh bootstrap), **hue-kit** (Philips Hue layered-scene framework: bridge sync, YAML scenes, meta-group solver), **llm-scripting-kit** (LLM key resolution, shared model registry, and named OpenAI-compatible endpoints -- OpenRouter is the default endpoint; importable package `llm_scripting_kit`, CLI `llm-scripting-kit`), **p4-kit** (Perforce multi-agent code review), **prototypes** (experimental skills awaiting graduation), **skills-kit** (verb x artifact authoring/audit matrix for skills + CLAUDE.md, folded into a single domain-skill: /md-domain, plus knowledge-encoding, update-documentation, materialized-output), and **unreal-kit** (Unreal Engine Python API automation). Dev-only (not published, `published: false`): **agent-glue**, **workflow-kit**.

This repo is a **Claude Code plugin marketplace** — it extends Claude Code with skills, commands, and hooks via the `.claude-plugin/marketplace.json` manifest. Plugins are loaded either via `--plugin-dir` (local development) or `enabledPlugins` in settings (production installs from the remote repo).

## Architecture

```
plugins-kit/                          # Marketplace root
  .claude-plugin/marketplace.json     # Marketplace manifest (lists all plugins)
  plugins/
    bootstrap/                        # Bootstrap plugin (always enabled)
      .claude-plugin/plugin.json      # Plugin manifest
      bootstrap.json                  # Bootstrap plugin's own manifest
      engine/                         # Bootstrap engine + config
      bootstrap_lib/                  # Shared libraries (cache, tool_check, etc.) — installable Python package
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
| `plugins/bootstrap/engine/bootstrap_engine.py` | Main engine — processes manifests, runs checks, emits hook JSON |
| `plugins/bootstrap/bootstrap_lib/cache.py` | Content-hash caching (compute, check, write) |
| `plugins/bootstrap/bootstrap_lib/tool_check.py` | System tool availability checks |
| `plugins/bootstrap/bootstrap_lib/platform_detect.py` | OS detection |
| `plugins/bootstrap/bootstrap_lib/log.py` | File-based bootstrap logging |
| `plugins/bootstrap/bootstrap_lib/venv_check.py` | Python venv validation |
| `plugins/bootstrap/bootstrap_lib/git_dep_check.py` | Git dependency validation |
| `plugins/bootstrap/bootstrap_lib/plugin_resolve.py` | Plugin registry resolution |
| `plugins/bootstrap/bootstrap_lib/path_check.py` | PATH entry validation |
| `plugins/bootstrap/bootstrap_lib/manifest_merge.py` | Deep-merge for layered bootstrap.json files |
| `plugins/bootstrap/engine/config.py` | Config loading, migration, persistence |
| `plugins/bootstrap/hooks/sessionstart/session-bootstrap.sh` | SessionStart hook (bash wrapper for engine) |
| `plugins/bootstrap/bootstrap.json` | Bootstrap plugin's own manifest |
| `plugins/bootstrap/skills/bootstrap/references/engine-internals.md` | Bootstrap engine internals |
| `docs/planning/bootstrap/MILESTONES.md` | Development milestones and progress |
| `tests/bootstrap/` | All bootstrap tests (mirrors bootstrap_lib/ structure) |

### Key Design Decisions

- **Bootstrapping**: Two-layer system — session bootstrap (bash SessionStart hook, manifest-driven) ensures system tools, venv, and git deps; script bootstrap (Python, runs inside UE Editor) handles UE-side packages at runtime. See [engine-internals.md](plugins/bootstrap/skills/bootstrap/references/engine-internals.md) for engine details and [script-bootstrap.md](plugins/unreal-kit/skills/ue-python-api/references/script-bootstrap.md) for UE-side bootstrapping.
- **Config resolution order**: CLI args > per-project config (`<project_root>/.claude/unreal-kit.yaml`) > global config (`~/.claude/plugins/data/plugins-kit/unreal-kit/config.yaml`, legacy fallback) > skill config (`ue_runner_config.yaml`) > hardcoded defaults
- **Auto-detection execution**: `ue_runner.py` tries remote execution (UDP via upyrc) first, falls back to headless commandlet if editor isn't running

### Unreal Engine work

For UE Python automation (running scripts, the `ue_runner` host-side runner, in-editor patterns, sys.path conventions, dependency bootstrap), invoke the `ue-python-api` skill at `plugins/unreal-kit/skills/ue-python-api/SKILL.md`.

## Bootstrap (foundation for all plugins)

The **bootstrap** plugin is the dependency-management layer every other plugin in this marketplace rides on. Claude Code runs it via a SessionStart hook at the start of every session; bootstrap then reads each enabled plugin's `bootstrap.json` and ensures system tools, venvs, git deps, marketplaces, and per-user config are in the state the plugins need. **No bootstrap, no working plugins.** When a `bootstrap.json` changes (e.g. a new Python dependency, a new check), the next session that actually runs bootstrap will apply those checks and remediate.

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
- A manual `bootstrap-reset-cooldown` is needed only after editing a *layered* `bootstrap.json` (`~/.claude/` or `<project>/.claude/`), which touches no registry file.
- Provisioning is done once `engine_ran_version` == installed version; a restart is then needed only to load new plugin CODE. `engine_ran_version` staying behind after a restart + a prompt is an **anomaly to surface**, not success.
- Mid-session installs (`/plugin` + `/reload-plugins`) also converge without a restart -- a fresh plugin's venv exists a prompt or two later.

### Anti-pattern: repairing a wedged machine by hand

**Our job is not to fix bootstrap issues. It is to make bootstrap fix them.** A machine that is wedged is a *specification* for a repair that ships; it is not a chore to clear. Whenever you find yourself typing the command that unwedges the machine in front of you, you are writing the wrong artifact.

A hand-repair fails twice over:

1. **It converges nobody.** The machine in front of you is one of N. Every other user with the same wedge is still stuck, and nothing you did will reach them. Only a published change to `bootstrap` (or `bootstrap-stuck-fix`, per the escape-hatch test below) reaches anyone.
2. **It destroys the evidence.** A wedge is usually only observable while it is happening. Repairing it overwrites the registry, the cache tree, and the marketplace clone -- the exact state needed to diagnose it. A hand-repair therefore converts a diagnosable defect into a permanently unexplained one, guaranteeing the recurrence it appeared to resolve.

The second cost is the one that gets underestimated, because the machine looks *better* afterwards. It isn't. A healthy machine with an unknown root cause is strictly worse than a wedged machine you can still read.

**Worked example (2026-07-27).** Nine plugins reported `Plugin "<name>" not cached at <path>` in the Claude Code plugin list; a restart did not clear it. The on-disk state was inspected and found coherent -- every `installPath` in `installed_plugins.json` existed, cached `plugin.json` versions matched the registry, and the registry matched the marketplace clone -- so the cause was in Claude Code's cache resolution and was not reproducible from disk. At that point the correct move was to snapshot the state and ship a repair. Instead the engine was invoked by hand:

```bash
# ANTI-PATTERN -- do not do this to a wedged machine before capturing its state
python .../bootstrap/0.63.0/engine/bootstrap_engine.py \
  --plugin-root .../bootstrap/0.63.0 --data-dir .../data/plugins-kit/bootstrap \
  --project-dir <project> --console
```

The pass fetched the marketplace, installed 0.64.1 into the cache, and rewrote the registry, and the wedge stopped being observable.

Read the consequences carefully, because they are the reason this is an anti-pattern rather than a shortcut:

- **The root cause is permanently unrecoverable.** The failing state existed only while it was failing. It cannot be reconstructed, so no repair can be written and no test can be built against it.
- **Nothing shipped.** Every other machine that hits the same wedge is still wedged. The one machine that could have specified the fix was spent clearing itself.
- **The "fix" was never even verified.** Afterwards, only bootstrap was confirmed to load (its hook fired at 0.64.1). The other eight affected plugins were never re-checked. "The machine recovered" was asserted, believed, and written into documentation without evidence -- a hand-repair produces a *feeling* of resolution that outruns what was actually established.
- **The causal link is unproven.** What the pass did -- a routine version update -- has no evident connection to a cache-resolution error. It is entirely possible the wedge cleared for an unrelated reason, which means even the folk remedy this produced ("run the engine by hand") may be worthless.

The wedge was a specification. It was spent as a chore, and it did not even demonstrably complete the chore.

**Both entry points are the anti-pattern.** This applies equally to the engine (`bootstrap_engine.py`) and to the hook (`plugins/bootstrap/hooks/sessionstart/session-bootstrap.sh`). Hand-invoking either runs a full live pass outside the conditions bootstrap is designed for, so what you observe generalizes to nobody. (An earlier insight in this file, `run_bootstrap_hook_directly`, advised the opposite; it is retracted and replaced by `never_run_bootstrap_hook_directly`.)

**The legitimate way to make bootstrap run again** is to clear the throttle and let a real session start do the work:

```bash
bash plugins/bootstrap/scripts/bootstrap-reset-cooldown.sh   # then start a session
```

That exercises the same code path under the same conditions a user gets, so the result means something. Resetting the cooldown is not the anti-pattern -- forcing the pass yourself is.

**The discipline.** When a machine is wedged:

1. **Snapshot first, always.** Capture `installed_plugins.json` verbatim, the `~/.claude/plugins/cache/` tree listing, each marketplace clone's HEAD sha and `git status`, `enabledPlugins` from user and project settings, and the Claude Code version. This costs seconds and is the only artifact that survives the repair.
2. **Then write the repair into the plugin**, choosing `bootstrap` vs `bootstrap-stuck-fix` by the escape-hatch test below.
3. **Let the mechanism heal the machine.** The wedged machine is the integration test for the repair. Unwedging it by hand forfeits that test.

A hand-run engine invocation is a diagnostic of last resort, valid only *after* step 1 -- and even then, prefer a read-only probe over a full pass.

**`--console` is not read-only.** It suppresses log-file writes and JSON output; it does **not** suppress provisioning. A `--console` pass still fetches marketplaces, installs plugin versions into the cache, and rewrites `installed_plugins.json`. Do not reach for it as a safe way to look at a wedged machine -- it is a live pass with quieter output. (This misreading is what turned the 2026-07-27 investigation above into a repair.)

**Bootstrap cannot patch itself -- ship the escape hatch in `bootstrap-stuck-fix`.** When a bug is in the *delivery path* (update, harvest, registry record selection, install scope), fixing it in bootstrap is a no-op for everyone it affects: the fix reaches a machine only by the mechanism that is broken there. Publishing it looks like progress, converges nobody, and strands every LATER bootstrap fix behind the same wedge. Such bugs are also self-masking -- the machine reports one stable error forever, so it reads as a known annoyance rather than a stuck update.

Test before writing the fix: *would this change have to be installed by the thing it repairs?* If yes, the repair belongs in `plugins/bootstrap-stuck-fix/` -- a separate, dependency-free plugin with no prior version to be wedged on, so it runs current code on its first session. Fix the root cause in bootstrap too, for machines that are not yet stuck; just do not mistake that for remediation of the ones that are. See that plugin's README for the two defects covered and the narrowness discipline every remediation there follows (act on one exact shape, never force a version, never break a session).

For deeper material -- manifest schema, condition categories, fix-all flow, engine internals -- invoke `/bootstrap`.

## Development Workflow

**Automated tests required** — every new module or integration point must have corresponding tests in `tests/` before the work is considered complete. Test directories mirror the plugin structure (e.g. `tests/bootstrap/` for the bootstrap plugin). This standard was established with the bootstrap plugin's M1 test suite and applies to all subsequent development.

**Targeted test runs** — the full test suite is too slow for routine use. Always run only the specific test file(s) relevant to your changes:

```bash
# Run a specific test file
uv run --extra dev pytest tests/bootstrap/test_marketplace_lifecycle.py -v

# Run a specific test class
uv run --extra dev pytest tests/bootstrap/test_marketplace_lifecycle.py::TestCheckPluginScope -v
```

Only run the full suite (`uv run --extra dev pytest -v`) when explicitly asked or before a release.

**Interpreter: the repo is pinned to Python 3.12** via a repo-root `.python-version`, so bare `uv run` / `uv venv` select 3.12 everywhere — no `-p 3.12` needed. Nothing needs 3.14 (four plugins exclude it: `requires-python ">=3.12,!=3.14.*"`); it used to leak in only as uv's global default when no pin was present.

The two formerly-documented "pre-existing failure" clusters (the `tests/skills-kit/` collection errors and the bootstrap `engine`/`venv` `CalledProcessError`s) were **fixed**, not version quirks — both were test-only issues: skills-kit imported the pre-extraction `schemas`/`_shared` modules, and the bootstrap tests spawned WSL `bash` to `source` a Windows env file and didn't isolate `HOME`. The full suite is green; investigate any failure as a real regression.

**Local development** — use `--plugin-dir` to test plugins from the working copy:

```bash
claude --plugin-dir ~/Dev/plugins-kit/plugins/my-plugin
```

`--plugin-dir` loads the plugin directly from disk (no cache copy) and makes no persistent changes — it doesn't modify `installed_plugins.json`, the cache, or `known_marketplaces.json`. Ending the session reverts to the marketplace-installed version.

**Reload vs restart (measured — see [plugin-reload-lifecycle.md](plugins/bootstrap/skills/bootstrap/references/plugin-reload-lifecycle.md)).** Three layers, not one rule: (1) a hook/engine/skill's **script content** is read fresh from disk on every invocation, so editing it is live with no reload/restart; (2) **registration** (`hooks.json` command map, which skills/commands exist) is reloaded **in-session by `/reload-plugins`** — including a changed hook command (the old "hooks require a full restart" claim is wrong as a blanket rule); (3) a **`SessionStart`** hook's registration reloads but it only **re-fires on a new session**, so re-running bootstrap's pass needs a restart. For a **real version update** (cache version dir moves), restart Claude / your IDE — it re-resolves install paths and re-fires SessionStart reliably.

**Publishing changes** — the plugin cache syncs from the remote repository's default branch, not the local working copy. Develop on the `dev` branch; merge to `master` only when releasing a version bump. This prevents the silent divergence explained under **The cache keys on version** below.

**How.** Commit your code and the version bump on `dev`, then:

```bash
uv run python scripts/publish.py            # preflight, publish, verify
uv run python scripts/publish.py --check    # preflight only; no writes, no pushes
```

**`scripts/publish.py` is the source of truth for the flow** — steps, guards, and post-verification live in code so this file cannot drift from what actually runs. Read its module docstring for the mechanics. Do not hand-run the steps; the script exists because three of them are easy to get wrong in ways that fail silently (a half-restored dev-tree that makes your next session load plugins from the working copy; a merge that publishes a dev-only plugin; an `index.html` that lands outside the release commit).

**Definition.** "Publish" means **all** of: version bump + regenerated `marketplace.json`, regenerated `index.html` inside the release commit, `dev` pushed, and `master` fast-forwarded and pushed. Anything less is not a publish -- a bump without the master merge, a bare `git push`, or a master merge without a bump each leaves consumers on the old release (`master` is the cache source, and the cache keys on version). `publish.py` refuses each of these rather than half-shipping.

`.claude-plugin/marketplace.json` is **derived data** — rebuilt from each plugin's `plugin.json`, filtered by `"published"` (missing = `true`; `false` = excluded). Never hand-edit its plugin entries; the pre-commit hook rejects drift.

**What the script will NOT do:** decide what ships. When `origin/master..dev` holds commits touching a dev-only (`published: false`) plugin, it refuses and names them — branch from master and cherry-pick the publish-ready commits yourself.

Publishing is reversible-but-visible: nothing is destroyed, but it goes out to other machines. The bar is "user has expressed publish intent for this work," not "user has reconfirmed each git command." Treat unambiguous go-signals — `go`, `ship it`, `publish`, `do it`, `close the loop`, `push` — as authorizing the whole flow; run `publish.py` and let its preflight be the safety net. Confirm only when intent is genuinely ambiguous (partial work, no version bump in sight, unrelated WIP staged, or the user is mid-thought).

After publish:

- Users with `autoUpdate: true` receive the update on next session start.
- Users without auto-update run `/plugin marketplace update` then `/plugin update`.

### The marketplace landing page (`index.html`) — regenerate at publish time

The repo-root **`index.html`** is the marketplace's public landing page (the GitHub-Pages-style poster listing every plugin and its skills). It is **generated, not hand-edited** — by awesome-kit's plugin-ecosystem skill. Regenerate it with:

```bash
python plugins/awesome-kit/skills/plugin-ecosystem/scripts/generate.py \
  --marketplace plugins-kit --title "plugins-kit marketplace" \
  --output ./index.html --no-open
```

**It crawls installPaths, not the working directory.** `generate.py` reads `~/.claude/plugins/installed_plugins.json` and walks each plugin's **`installPath`**, filtered by `marketplace.json`. In a normal session those paths point at the **cache** (`~/.claude/plugins/cache/<mkt>/<plugin>/<version>/`), which only refetches from **master** — so a plain regen before the merge reproduces the *old* page. That constraint is what used to force the regen after the merge. **Registry v2 caveat:** newer Claude Code keeps that registry at `{"plugins": {}}`; since awesome-kit 0.10.0 `generate.py` falls back to scanning the cache layout for refs the registry doesn't record (see the `registry_v2_empty` insight below), so a normal-mode regen renders the machine's cached plugins rather than an empty page.

**At publish time this is `publish.py`'s job — don't hand-run it.** The script repoints installPaths at the working copy via `dev-tree.py` (which, since the 0.47.0 release, also **synthesizes** entries for repo plugins the registry doesn't record — the registry-v2 case), regenerates, restores in a `finally`, and post-verifies the restore landed. It also lands `index.html` *inside* the release commit, so master is never in a state where its page disagrees with its own `marketplace.json`. The manual sequence below is for **previewing** only.

**Previewing the page by hand** (dev-tree regen, the load-bearing `--public`/`--marketplace-json` flags, always-restore discipline, preview-vs-publish commit rule): see [docs/reference/publish-reconcile.md](docs/reference/publish-reconcile.md). The one rule that must stay front-of-mind: if you ever run `dev-tree.py dev` by hand, run `normal` in the same breath -- leaving dev-tree mode on silently repoints every plugin at the working copy for all later sessions.

### Dev-only plugins — do not publish to master

Some plugins live on `dev` for in-development work and must not reach consumers until they are ready. Each such plugin sets `"published": false` in its `plugins/<name>/.claude-plugin/plugin.json`. The marketplace regenerator (`scripts/regen_marketplace.py`) filters those plugins out of `marketplace.json`, so they are excluded structurally — not by memory — even if their files land on master via a cherry-pick.

`publish.py`'s preflight **refuses** when `origin/master..dev` holds commits touching one, naming the commits. That is a backstop for the listing *and* the files; the regenerator only filters the listing. Picking which commits ship stays a human judgement call — branch from master and cherry-pick.

**Current dev-only plugins** (the field, not this list, is load-bearing — this is just a human-readable inventory):

- (none — `agent-glue` and `workflow-kit` graduated to published 2026-07-28; the mechanism above stays for future dev-only plugins.)

When you see commits for a dev-only plugin in `git log origin/master..origin/dev`, that's still gotcha 1 territory — branch from master, cherry-pick only the publish-ready commits, and leave the dev-only commits on `dev`. The regenerator is a backstop for the marketplace listing, not a substitute for picking the right commits to merge.

### dev -> master reconcile: conflict-resolution policy

For a full `dev`/`master` reconcile, resolve **toward dev** (master's divergent commits are prior publish artifacts dev supersedes) -- with one guard: back-port any master-only `+` lines in non-generated text to dev FIRST, then clobber. Full policy and mechanics: [docs/reference/publish-reconcile.md](docs/reference/publish-reconcile.md).

### Pre-publish validation (default)

**Default gate: before any publish, smoke-test the dev working copy with `claudx`.** `claudx` (defined in `~/.bashrc`) launches a `claude` session loading every `plugins/<name>` dir via one `--plugin-dir` each, so the session runs each plugin's skills/hooks/engine **code** straight from disk — no cache, no `installed_plugins.json` change, reverts on exit. Run it, exercise the changed surface (invoke the skill, trigger the hook, run the command), confirm it behaves, then publish.

```bash
claudx        # claude + --plugin-dir for every plugins-kit plugin (see ~/.bashrc)
```

**Known blind spot — manifest content.** Under `--plugin-dir`, the bootstrap engine still reads each plugin's `bootstrap.json` from its **cached** `installPath`, not from disk (insight `plugin_dir_doesnt_test_cross_plugin`). So `claudx` validates code paths but **not** new `bootstrap.json` content (added tools, `download:` recipes, `venv.check_imports`). When your change touches manifest content, escalate to **`claude-dev`** (also in `~/.bashrc`) — it uses `scripts/dev-tree.py` to repoint installPaths at the dev tree, so the engine loads `bootstrap.json` from disk too, then auto-restores normal cache mode on exit.

| Change touches… | Default validator |
|---|---|
| skills / hooks / commands / engine code | `claudx` |
| `bootstrap.json` / manifest content | `claude-dev` (dev-tree mode) |

**Bypassable at your discretion.** This is a default, not a hard gate. Trivial changes — a version-only bump, a doc/CLAUDE.md edit, a single-file mechanical fix — don't need a smoke session; skip it and say so. An unambiguous publish go-signal does not silently waive validation, but you may explicitly bypass when the change can't plausibly break a runtime surface.

### Anti-pattern: creating a branch, or switching the one that is checked out

**Stay on `dev`. Do not create branches, and never run `git checkout` / `git switch`
to move the working tree onto another branch.** Work here happens in ONE shared
working tree that more than one agent session may be using at the same time, and the
checked-out branch is global to that tree. Moving it is not a local decision — it
silently reaches into every other session running in this directory.

The failure is not that a branch is untidy; it is that **another session's commits
land on your branch instead of `dev`**, and it happens with no error and no warning.
The other session keeps working, runs `git commit`, and git faithfully commits to
whatever branch the tree is on. If your branch was cut from `master` (the natural
choice for a review or a cherry-pick), those commits are now parented on `master` and
have silently lost every `dev` commit beneath them.

**Worked example (2026-08-08).** A `review-bootstrap-cli` branch was created off
`origin/master` to scope a code review to two commits, avoiding the 15 unrelated
commits sitting in `origin/master..origin/dev`. The intent was good and the review
itself was scoped correctly. But `git checkout review-bootstrap-cli` moved the shared
tree, and a concurrent session then committed twice — `agent-glue: state the consumer
feedback as requirements` and `repo: drop incidental references to a private consuming
project`. Both landed on the throwaway master-based branch. `git branch --contains`
confirmed they existed on that branch and nowhere else: two commits of another
session's work, stranded, one `git branch -D` away from being unreachable.

Recovery took a commit of in-flight work, three cherry-picks, a content-identity check
per commit, and a force-delete. Nothing was lost, but only because the branch was still
there to find. That is the good outcome, not the expected one.

**Scope a review or a diff with a range, never with a branch.** `git log`, `git diff`,
and `prepare_review.py` all take `<a>..<b>` / `<a>...<b>` and read history without
touching the tree. Path scoping (`-- plugins/foo/`) narrows further. If commits are
non-contiguous, review each one individually (`<sha>^..<sha>`) — several small reviews
beat one branch switch. When isolation genuinely requires a separate checkout, use
`git worktree add` (a second directory, the shared tree untouched), never a branch
switch in this one.

**Publishing does not need a branch either.** `publish.py` owns the `dev` -> `master`
flow. The one case that historically wanted a feature branch — gotcha 1, cherry-picking
past unrelated `dev` commits — is a decision to escalate to the user, not to solve by
creating a branch yourself.

### Safe-publish practices

Publishing is the riskiest moment in this repo because it broadcasts to every consumer. Two failure modes have happened, both recoverable but visible (the retraction commits in `git log master` are the scars). Avoid them with these checks.

**Gotcha 1: fast-forwarding dev → master sweeps unrelated commits.** `dev` typically contains in-flight work from other plugins. A fast-forward merge ships *everything* between `master` and `dev`, not just your feature. **Mandatory check before any dev → master merge:**

```bash
git fetch origin
git log --oneline origin/master..origin/dev
```

If that list contains anything beyond the commits you intend to publish, **stop** — do not fast-forward. Pick a safe path instead:

1. **Branch from master, cherry-pick, PR to master.** Cleanest when dev has unrelated WIP. `git checkout -b <feature> origin/master`, cherry-pick the feature commit(s), push, open a PR. Doesn't touch `dev`. After master merges, merge master back into dev to keep dev current.
2. **Wait for the other dev work to ship first.** If those commits are nearly ready, finish their version bumps and publish them properly (every plugin you're shipping needs its own `plugin.json` + `marketplace.json` bump — without that, fresh installs silently diverge). Then publish your feature on top.
3. **Squash-merge a feature branch.** Same as (1) but one squashed commit on master.

Fast-forward `dev` → `master` is only safe when `git log origin/master..origin/dev` shows *exactly* the commits you intend to publish.

**Gotcha 2: `git add <file>` sweeps pre-existing working-tree modifications.** If a tracked file already had uncommitted local edits and you touch it for your feature, `git add <file>` stages *all* the changes in that file, not just yours. The feature commit then ships unrelated WIP. **Mandatory check before any publish commit:**

```bash
git diff --staged
```

Read every line. If anything is unrelated to the feature, `git restore --staged <file>` and use `git add -p` (or `git stash` the WIP first) to stage only the intended hunks. Same discipline for untracked files — don't `git add .` from a dirty tree.

*Sharpening — the dev tree is a live workspace.* The index may already hold **another session's** (or your own earlier) staged work before you touch it. `git add <your specific files>` followed by `git commit` commits the **entire index**, not just the files you named — so a pre-staged rename or WIP rides along under your commit message. The `git diff --staged` check above is the only guard: run it every time and confirm the staged set is *exactly* your files, even when you used a targeted `git add`. (This is how a `workflow-glue → workflow-kit` rename once landed inside an unrelated test-coverage commit.)

**Gotcha 3: a botched publish burns the version number.** Cache entries on consumer machines key off `(plugin, version)`. If a bad version is pushed to master, retracting it doesn't evict caches that already pulled it — same version = same code, forever, from the cache's view. The fix is a patch-bump *past* the burned number (e.g. 0.11.0 broken → don't ship 0.11.1, jump to 0.12.0) so every consumer's cache invalidates cleanly. The 0.11.1 / `patch-bump 4 plugins to force-refresh post-retraction caches` commits on master are an example of this recovery pattern.

**Gotcha 4: unauthorized publish.** The go-signal rule above scopes authorization to the work the user actually approved -- it does **not** authorize sweeping in adjacent unrelated work that happens to be staged or sitting on `dev`. A clean feature commit next to unrelated dev commits is gotcha 1 territory: branch from master.

**Recovery: how to retract.** A bad publish on master is fixed forward, never with `push --force` to master. Push a follow-up commit that either (a) reverts the bad commit and patch-bumps the affected plugins past the burned version, or (b) re-implements correctly under a new version. Consumers with `autoUpdate: true` then refresh on their next session start. Never rewrite master history — other machines have already fetched it.

**Master drifts behind dev on non-plugin infra — reconcile periodically.** The publish flow carries feature commits only, so master silently falls behind dev on repo infrastructure (gotchas, tests, tooling). Expected, not a bug; sync it from time to time with the infra-drift procedure in [docs/reference/publish-reconcile.md](docs/reference/publish-reconcile.md) (no version bumps, consumers unaffected).

**The cache keys on version** — same version = same code; the cache never refreshes without a bump, and fresh installs between releases copy HEAD code under the old version string (**silent divergence**). Consequences: `plugin.json` and `marketplace.json` versions must move together (the regenerator + `scripts/pre-commit-version-check.sh` enforce this); **manifest edits count as code edits** (a `bootstrap.json` change without a bump is structurally invisible to consumers -- see the `manifest_changes_need_version_bump` insight below); never copy files directly into the plugin cache; and don't omit the version field hoping for rolling updates (Claude Code substitutes a git SHA that becomes a static cache key anyway).

**Keep architecture docs current** — when modifying bootstrap behavior, update the bootstrap skill references (`plugins/bootstrap/skills/bootstrap/references/`) to reflect the changes. These are the source of truth for how the system works.

**Anti-pattern: silent bootstrap operations.** Every bootstrap check must log its outcome — `ok_entries` when passing (verbose-only), `action_entries` when remediating (always visible). Adding a check that creates files, clones repos, or writes config without emitting a log entry is a bug. See the "Every check must log its outcome" principle in [engine-internals.md](plugins/bootstrap/skills/bootstrap/references/engine-internals.md).

**Always use `uv run python` in shell scripts** — never bare `python` or `python3`. On Windows, the system PATH contains Microsoft Store stubs (`WindowsApps/python.exe`) that take precedence over any user PATH entry, causing bare `python`/`python3` to fail with "Permission denied" (exit 126) in Git Bash. On macOS, bare `python` often doesn't exist. Since bootstrap guarantees `uv` is available, `uv run python` is the standard way to invoke Python from any shell script in this project. It resolves the correct Python, activates the venv (giving access to installed packages), and works on all platforms.

**Plan non-trivial tasks**: Plan when both (a) the task is non-trivial, and (b) the implementation could go several reasonable directions. Share the plan, get a thumbs-up, then implement. Skip planning when the path is obvious or the user has already framed the approach — in those cases extra ceremony reads as procedural friction, not rigor. When you do plan, use plan mode (`EnterPlanMode`) as the sanctioned space to think and propose; don't ritualize the steps. The goal is alignment on intent, not a checklist.

**Skill-based document placement** (package cohesion): when creating a document, ask "what skill does this belong to?" and place it by the CCP/CRP/ADP framework -- `plugins/skills-kit/skills/md-domain/references/cohesion-principles.md` is the SSOT for those principles and the placement algorithm. If no existing skill fits, create a stub skill and let the document live as a progressively-disclosed reference inside it.

**Plugin boundaries are hard boundaries for cohesion work.** Never move content between plugins — or into a new plugin — to achieve skill cohesion. Plugins are independently versioned, installed, and bootstrapped units; relocating a skill/reference across a plugin boundary to satisfy CCP/CRP/ADP breaks that independence (cross-plugin caches, dependency edges, version coupling) and is never worth the cohesion gain. Cohesion refactors operate *within* a plugin only. When you spot a genuine cohesion opportunity that spans plugins — two doer-skills in different plugins sharing a subject (e.g. git-kit `git-code-review` + p4-kit `p4-code-review`), a reference duplicated across plugins, a shared substrate two plugins both consume — **surface it as an insight** (a `claude_md:` insight or a note in the relevant skill), do **not** act on it by relocation or by spawning a unifying plugin. Sharing across plugins is done through a library both depend on (e.g. `bootstrap_lib.code_review`), not by merging the skills.

**Reference file design** (within a skill): each reference serves a single audience and changes for a single reason (same cohesion framework). See `plugins/bootstrap/skills/bootstrap/` for the gold standard -- references split by audience with clean change boundaries.

## Plugin System

Plugins follow the Claude Code plugin spec:
- **Marketplace manifest** (`.claude-plugin/marketplace.json`): Lists available plugins with name, version, source path
- **Plugin manifest** (`.claude-plugin/plugin.json`): Per-plugin metadata (name, version, description, keywords)
- **Skill discovery**: Claude Code scans `skills/` directories for `SKILL.md` files
- **Variable expansion**: `${CLAUDE_PLUGIN_ROOT}` resolves to the plugin's install path at runtime

### Plugin dependencies on bootstrap (declared + guarded)

Every plugin in this marketplace rides on **bootstrap** (venv, `bootstrap_lib`, `uv`, installed config). We make that dependency explicit in **two complementary layers**:

1. **Declared dependency (install-time).** The Claude Code plugin spec supports inter-plugin dependencies — installing a dependent auto-installs/enables its dependencies, blocks disabling a still-needed dependency, and honors version constraints. Every plugin that depends on bootstrap declares it in its `.claude-plugin/plugin.json` as a **bare string** (bootstrap lives in the *same* marketplace, so `name` resolves within `plugins-kit`):
   ```json
   "dependencies": ["bootstrap"]
   ```
   This is the canonical fix for "user installed the plugin without bootstrap." Official docs (source of truth — fetch when in doubt): https://code.claude.com/docs/en/plugin-dependencies and the `dependencies` field in https://code.claude.com/docs/en/plugins-reference.
   - **Same-marketplace deps are bare strings.** Do NOT add a `"marketplace"` field for a dep in this marketplace — that field is *only* for a **different** marketplace and triggers the `allowCrossMarketplaceDependenciesOn` allowlist (a same-marketplace value gets treated as cross-marketplace and can fail installs).
   - **Unversioned on purpose.** A version constraint (`{ "name": "bootstrap", "version": "~0.12" }`) resolves against `{plugin}--v{version}` git tags (`claude plugin tag --push`), which this repo does not use — pinning would cause `no-matching-tag`. Bare = "whatever the marketplace provides."
   - Declare it on every plugin that has a `bootstrap.json` **except** bootstrap itself. Plugins with no `bootstrap.json` (e.g. `cache-kit`) genuinely don't depend on bootstrap — do not add the field. Enforced at pre-commit by `scripts/check_bootstrap_dependency.py` (chained from `pre-commit-version-check.sh`; spec mirrored in `tests/repo-scripts/test_bootstrap_dependency.py`).
   - It belongs in **both** `plugin.json` and the generated marketplace entry; `scripts/regen_marketplace.py` propagates it automatically. A `dependencies` edit is a manifest change: it needs a version bump to reach consumers (same rule as any `plugin.json`/`bootstrap.json` edit).

2. **Runtime guard (provision-time).** A declared dependency guarantees bootstrap is *installed*, not that it has *run* — on first install bootstrap provisions each plugin's venv at the next SessionStart (and the cooldown can defer it). For that "installed-but-not-yet-provisioned" window, plugins that would otherwise crash with a raw `ModuleNotFoundError`/missing-interpreter error use the vendored **`bootstrap_guard.py`** (canonical: `plugins/bootstrap/bootstrap_lib/bootstrap_guard.py`). It is **stdlib-only** and **must never import `bootstrap_lib`** (that's the thing that may be missing); it detects absence via the per-plugin `~/.claude/plugins/data/<marketplace>/<plugin>/bootstrap.log` and exits with one actionable "install/enable plugins-kit:bootstrap" message instead of a raw traceback. It is **vendored** per plugin (copied next to the entry script and imported as a plain module), exactly like `path_repair.py`, with a drift test asserting copies match the canonical.

### Hook JSON Format & Plugin Cache Layout

The Claude Code hook-JSON output contract (exit-code semantics, universal fields, the per-event decision table, the `hookSpecificOutput` rule) and the on-disk plugin cache/registry layout (`~/.claude/plugins/` paths) are static CC platform reference — see [docs/reference/claude-code-plugin-platform.md](docs/reference/claude-code-plugin-platform.md). Canonical upstream: https://code.claude.com/docs/en/hooks.

The one plugins-kit-specific wrinkle to keep in mind: bootstrap runs in a **background mode** — the engine writes output to a pending file that the UserPromptSubmit hook re-emits as its own stdout, because Stop hooks don't support `hookSpecificOutput` (so UserPromptSubmit carries the `additionalContext` for Claude).

### Debugging

```bash
# Version report — shows local, marketplace, installed, and cached versions for all plugins
bash scripts/plugin-versions.sh

# Run bootstrap engine in console mode (plain text, no JSON, no log writes)
# WARNING: --console is NOT read-only. It still fetches marketplaces, installs
# versions into the cache, and rewrites installed_plugins.json. Never point it at
# a wedged machine before snapshotting its state -- see the hand-repair
# anti-pattern in the Bootstrap section above.
python plugins/bootstrap/engine/bootstrap_engine.py --plugin-root plugins/bootstrap --data-dir ~/.claude/plugins/data/bootstrap --console

# Verbose mode (show ok/cached entries too)
python plugins/bootstrap/engine/bootstrap_engine.py --plugin-root plugins/bootstrap --data-dir ~/.claude/plugins/data/bootstrap --console --verbose
```

## Task folders are gitignored here

`dev/` is gitignored, and that **includes `dev/tasks/`**. Task folders in this
repo are working state, not repo content: they are local scratch for the agent
and the developer driving it, and they do not belong in the history other
people pull.

This is a deliberate deviation from `awesome-kit:task`'s model, which treats
`dev/tasks/<stub>` as the DURABLE half of the system ("version control is the
record") and `tmp/<stub>` as the ephemeral half. That contract does not apply
to this repo. Consequences to know rather than rediscover:

- The task CLI's validate/work verbs may warn that a `dev/tasks` folder is
  uncommitted, and `archive` on a dev/tasks folder expects to commit a final
  state and delete the folder. Here, treat those as noise -- the folder is
  intentionally invisible to git.
- **Do not add a `!dev/tasks/` negation to `.gitignore` to "fix" this.** It has
  been done once and reverted. (Note also that a negation under a bare `dev/`
  rule is inert: git cannot re-include a path inside an excluded directory, so
  the exception silently does nothing until someone also rewrites `dev/` to
  `dev/*` -- which is how the mistake looks like it worked.)
- A task folder's contents are therefore **one clean tree away from gone**.
  Anything that must outlive the task -- a spec, a decision record, a
  reference -- belongs in its owning repo at authoring time, which is what the
  task system's `durable_outputs` rule already requires. Follow that rule
  strictly here, because the folder is not a backstop.

## Preferences

- **No temporal deixis in documentation.** Never "recent(ly)", "new", "just
  shipped", or similar now-relative phrasing in docs, READMEs, or outward
  content -- state what a thing IS, cite a source, and use absolute dates
  when a date matters. Now-relative claims rot silently and are unverifiable.
- **Never use the memory system** (`~/.claude/projects/*/memory/`). Always update `CLAUDE.md` instead — it is machine-independent and checked into the repo, so all machines and sessions share the same context.

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
    excludes:
      - per-plugin internals (covered by per-plugin CLAUDE.md / bootstrap.json)
  insights:
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
      summary: "SUPERSEDES the former run_bootstrap_hook_directly guidance (2026-04-28), which was wrong. Do NOT hand-invoke session-bootstrap.sh or bootstrap_engine.py to force a pass. Reset the cooldown and let the next real session run it."
      detail: |
        The former guidance told you to invoke plugins/bootstrap/hooks/sessionstart/session-bootstrap.sh
        directly to force a refresh. Treat that as retracted. Hand-invoking either entry point --
        the hook or the engine -- runs a full live pass outside the conditions bootstrap is
        designed for, and it is how a diagnosable wedge gets converted into an unexplained one
        (see never_hand_repair_a_wedge and the anti-pattern section in the Bootstrap chapter).
        The legitimate way to make bootstrap run again is to clear the throttle and let a REAL
        session start do it:
          bash plugins/bootstrap/scripts/bootstrap-reset-cooldown.sh   # then start a session
        That path exercises the same code under the same conditions users get, so what you
        observe is what they would observe. A hand-invoked pass does not, and its success or
        failure generalizes to nobody.
        Narrow exception: a hand-invoked pass is a diagnostic of last resort, permitted only
        AFTER the machine's state has been snapshotted, and preferred only when no read-only
        probe would answer the question.
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
        MEASURED reload/restart rule (don't trust "hooks always need a restart" -- wrong):
        script CONTENT is read fresh from disk each run; REGISTRATION reloads in-session via
        /reload-plugins; only SessionStart re-firing needs a new session. Full mechanics, the
        convergence sweep, the reload-nag (_reload_advice), and the probe method:
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
      summary: Newer Claude Code keeps installed_plugins.json PERMANENTLY EMPTY ({"version":2,"plugins":{}}) for marketplace installs -- enablement lives in settings enabledPlugins, code in the cache layout. Everything that read the registry needed a cache-scan fallback (bootstrap 0.47.0) and a SessionStart that races the fresh-machine plugin sync is caught by the UserPromptSubmit rescue (0.46.0).
      detail: |
        Engine-side fixes (rescue 0.46.0, cache-scan fallback 0.47.0) are owned by the bootstrap
        skill references (engine-internals.md, plugin-reload-lifecycle.md) -- consult those for
        mechanics. The REPO-specific residue to remember here:
        - dev-tree.py must SYNTHESIZE entries for repo plugins the registry doesn't record;
          that synthesis is how publish.py's index.html regen works on v2 machines (the 0.47.0
          release briefly shipped an empty index.html before it existed).
        - awesome-kit's generate.py needed the same cache fallback (awesome-kit 0.10.0,
          merge_cache_fallback) or the poster renders empty.
        - Claude Code still WRITES enabledPlugins to the live ~/.claude/settings.json on
          `claude plugin install` -- check `git diff` before assuming settings.json's committed
          state matches the live file.
      origin: "Live fresh-machine testing 2026-07-16 (this machine, wiped ~/.claude/plugins repeatedly); fixed in bootstrap 0.46.0 (rescue) + 0.47.0 (cache fallback) + dev-tree.py synthesis."
      added: "2026-07-16"
    - id: host_python_via_plugin_venv
      keywords: [host-side python, plugin venv, uv run python, ModuleNotFoundError, foreign cwd, project root, pyyaml, skill examples]
      summary: SKILL.md examples that invoke host-side Python must use the explicit plugin-venv path, not `uv run python`, when the documented cwd is the user's project root.
      detail: |
        `uv run python` resolves the venv from the cwd's pyproject.toml. When a skill instructs
        the user to run from a project root that has no matching pyproject.toml (e.g. an
        Unreal project root, where p4 picks up .p4config.txt), uv falls back to a bare
        interpreter without the plugin's installed dependencies and the script crashes with
        ModuleNotFoundError. Bootstrap installs each plugin's venv at a stable canonical path:
          Windows: ~/.claude/plugins/data/<marketplace>/<plugin>/.venv/Scripts/python.exe
          macOS/Linux: ~/.claude/plugins/data/<marketplace>/<plugin>/.venv/bin/python
        The path does not change across plugin versions and resolves correctly from any cwd.
        Use it directly in SKILL.md examples instead of `uv run python`.

        Script-side self-defense (the code that consumes a shared lib) is a plugin
        implementation detail -- see plugins/CLAUDE.md "Shared-lib scripts must re-exec
        under the plugin venv" for the reexec_under_plugin_venv rule that makes a
        standalone script invocation-method-agnostic.
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
      keywords: [--plugin-dir, claudx, smoke test, cross-plugin, bootstrap testing, installPath, dev tree, cache, layered manifests]
      summary: --plugin-dir overrides Claude Code's load of one plugin from disk, but the bootstrap engine's per-plugin iteration still reads OTHER plugins' bootstrap.json from their cached installPath.
      detail: |
        Loading a plugin via `--plugin-dir <dev tree>` only overrides Claude Code's loading of
        THAT plugin's hooks/skills. The bootstrap engine's per-plugin loop iterates
        `installed_plugins.json` and reads each plugin's bootstrap.json from its cached
        installPath. So when claudx loads every dev plugin via --plugin-dir, the engine
        still sees each plugin's CACHED bootstrap.json -- not the dev-tree version.
        Implication: --plugin-dir smoke tests can exercise the new engine code paths (the
        engine binary is loaded from dev), but they cannot exercise new bootstrap.json content
        for any plugin without first publishing that plugin. Workarounds: (a) bump versions
        and publish to test for real; (b) use the `pk-dev` mode helper, which rewrites
        installed_plugins.json to point installPaths at the dev tree -- that does exercise
        new bootstrap.json content; (c) test new bootstrap.json content via layered manifests
        in `~/.claude/bootstrap.json` or `<project>/.claude/bootstrap.json`, which DO go
        through the engine without an installPath lookup.
      origin: Surfaced 2026-05-27 -- the claudx smoke test couldn't validate jq's new download recipe because the engine kept reading the cached bootstrap.json.
      added: "2026-05-27"
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
        "Plugin boundaries are hard boundaries for cohesion work" above; (2) routing value is low
        -- git-vs-p4 is unambiguous from the workspace, so a natural-language front door adds
        little over the two already-auto-triggering skills. Correct cross-plugin sharing is the
        library both depend on (bootstrap_lib.code_review), which already exists. Do not
        re-investigate a code-review domain; the answer is "surface, don't merge."
      origin: Surfaced 2026-05-31 during the cohesion refactor -- after W2-proper, an Explore feasibility sweep found the shared lib already exists; user ruled cross-plugin relocation/new-plugin out of bounds for cohesion work.
      added: "2026-05-31"
    - id: never_hand_repair_a_wedge
      keywords: [hand repair, manual fix, wedged machine, not cached, snapshot first, evidence destroyed, converges nobody, console not read-only, anti-pattern, ship the repair, diagnostic of last resort]
      summary: Never unwedge a machine by hand. A wedge is a specification for a repair that ships -- hand-fixing it converges nobody and destroys the only evidence of the defect. Snapshot the state first, then write the repair into bootstrap or bootstrap-stuck-fix.
      detail: |
        A hand-repair fails twice: it reaches only the machine in front of you, and it
        overwrites the registry/cache/marketplace state that is the sole record of the
        defect -- turning a diagnosable bug into a permanently unexplained one. The second
        cost is systematically underestimated because the machine looks healthier afterwards.
        Procedure when a machine is wedged: (1) snapshot installed_plugins.json verbatim, the
        cache tree listing, each marketplace clone's HEAD sha + git status, enabledPlugins
        from user and project settings, and the Claude Code version; (2) write the repair
        into the plugin, choosing bootstrap vs bootstrap-stuck-fix by the escape-hatch test;
        (3) let the mechanism heal the machine -- the wedged machine is the integration test
        for the repair, and unwedging it by hand forfeits that test.
        Load-bearing correction: `--console` is NOT read-only. It suppresses log writes and
        JSON output only; the pass still fetches marketplaces, installs versions into the
        cache, and rewrites installed_plugins.json. Misreading it as a safe probe is exactly
        what converted the 2026-07-27 investigation into a repair.
        Full narrative and the worked example: "Anti-pattern: repairing a wedged machine by
        hand" in the Bootstrap section above.
      origin: "User directive 2026-07-27 after nine plugins reported 'not cached' and the engine was run by hand to clear it -- the machine recovered, the root cause became unrecoverable, and no fix shipped to any other machine."
      added: "2026-07-27"
    - id: never_create_or_switch_branches
      keywords: [branch, git checkout, git switch, feature branch, create a branch, scope a review, cherry-pick branch, shared working tree, concurrent session, stranded commits, worktree, stay on dev]
      summary: Stay on dev -- never create a branch or move the checked-out branch. The working tree is shared with concurrent agent sessions, so a branch switch silently redirects THEIR commits onto your branch.
      detail: |
        The checked-out branch is a property of the one shared working tree, not of your
        session. Switching it reaches into every other session running in this directory:
        the other session commits normally, git writes to whatever branch the tree is on,
        and no error is raised. When the branch was cut from master -- the natural base for
        a review or cherry-pick -- those commits are parented on master and have silently
        lost every dev commit beneath them.
        Scope reviews and diffs with a RANGE (`<a>..<b>`, `<sha>^..<sha>`) plus path
        filters; git log / git diff / prepare_review.py all read history without touching
        the tree. Non-contiguous commits: review each individually rather than assembling a
        branch. If a separate checkout is genuinely required, `git worktree add` gives one
        without moving this tree. Publishing needs no branch -- publish.py owns dev -> master,
        and gotcha 1 (unrelated dev commits) is a decision to escalate to the user, not to
        solve by creating a branch.
        Full narrative and the worked example: "Anti-pattern: creating a branch, or
        switching the one that is checked out" in the Development Workflow section above.
      origin: "2026-08-08 -- a review-bootstrap-cli branch was created off origin/master to scope a code review; a concurrent session then committed twice onto it, stranding both commits on a master-based throwaway branch that git branch --contains showed existed nowhere else."
      added: "2026-08-08"
  conventions:
    - rule: Stay on dev -- never create a branch or run git checkout/switch in this working tree; scope reviews with a commit range, and use git worktree if a separate checkout is truly needed.
      keywords: [branch, git checkout, git switch, shared working tree, concurrent session, scope a review, range, worktree]
      why: The tree is shared with other agent sessions and the checked-out branch is global to it, so a switch silently redirects their commits onto your branch. See the never_create_or_switch_branches insight and the anti-pattern section in Development Workflow.
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
