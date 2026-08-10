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

Only run the full suite when explicitly asked or before a release -- and when you do,
**parallelise it**, because the full suite is the only run where that pays:

```bash
uv run --extra dev pytest -n 12 -q      # full suite, ~3 min
```

`pytest-xdist` is in the `dev` extra. `-n` is deliberately NOT in `addopts`, and adding
it there would be a regression rather than a convenience: worker startup is a fixed
~1.6-2.9s toll, which is free on a 13-minute run and ruinous on the targeted runs above.
Measured on a 24-core box, `tests/bootstrap/test_cache.py` alone costs **0.40s serial,
2.03s at `-n 12`, 3.33s at `-n auto`** -- so a config-level `-n` makes the tight TDD loop
5-8x SLOWER while making the full run faster. Pass `-n` explicitly, per run.

Pick the worker count deliberately too; more is not better. This suite is process-spawn
bound (real `git`, `uv`, and Git Bash subprocesses), so past a point extra workers just
contend for the same spawns. Full-suite wall time on that 24-core box: `-n 8` 4:00,
**`-n 12` 2:54**, `-n 16` 3:10, `-n auto` (=24) 3:21. Roughly half the core count is the
sweet spot; `-n auto` is portable but not optimal on a many-core machine.

Two consequences of parallelism worth knowing before you blame your change for a failure:

- **Timing-sensitive tests can fail under load and pass serially.** The SessionStart
  display hook spawns ~10 Git Bash processes and its foreground takes 11-22s on a
  saturated machine, against ~1s idle. `tests/bootstrap/test_sessionstart_rescue.py` is
  hardened for this (generous *polling* for positive assertions; a causal observable
  instead of a fixed sleep for negative ones). If you add a test that waits on a
  subprocess, follow that pattern -- never a bare `time.sleep` sized for an idle machine.
- **The three root-conftest leak guards run in ONE worker under `-n`** (see the comment in
  `tests/conftest.py`), because they snapshot machine-global state that xdist cannot
  isolate. Leak detection is therefore complete only in a SERIAL run. Run serially when
  the question is "is something leaking into my real `~/.claude`".

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

### Committing and pushing to `dev` is unrestricted -- only PUBLISHES are gated

**Standing policy. Commit and push to `dev` freely, without asking.** Do not hold
finished work back for a confirmation, do not present a commit as a proposal, and do
not ask whether to push. `dev` is a working branch: nothing on it reaches a consumer,
because `master` is the cache source. The only gated action in this repo is a
PUBLISH (`dev` -> `master` via `publish.py`), which broadcasts to every machine and
needs the user's intent per the go-signal rule above.

**Do not coordinate around other agent sessions.** The tree is shared and other
sessions commit, stage, and push concurrently. That is normal and is not your problem
to manage: do not wait for the tree to be clean, do not ask about someone else's
uncommitted work, do not treat unrelated commits riding along on a `git push origin dev`
as a reason to stop, and do not report another session's failing tests as if they were
your regression. Push carries whatever else is on `dev`; that is the intended
behaviour, not gotcha 1 (which is exclusively about `dev` -> `master`).

Two habits survive this policy, because they are hygiene rather than permission-seeking
and they cost seconds:

- **Scope the commit to your own files, by explicit path.** Not to protect the other
  session from you, but so `git log` stays readable and a revert stays surgical.
- **When the index already holds someone else's staged work, do not fight it.**
  `git commit -F <msg> -- <your paths>` commits the working-tree state of exactly those
  paths and leaves the index otherwise untouched, so you never have to unstage
  another session's work to get your own in. Reserve `git reset` for an index you own.

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

**Worked example (2026-08-08).** Six files were staged by explicit path for an
orchestrate change. `git diff --staged` showed roughly twenty more, including
`plugins/unreal-kit/skills/ue-python-api/stubs/unreal.py` staged as 588,614 deletions.
That file was `git restore --staged`-ed to get the commit scoped. It turned out to be a
deliberate `git rm --cached` -- another session was untracking a generated stub, paired
with a staged `.gitignore` change in the same index. Restoring it required inferring
that intent from the surrounding staged files and re-running `git rm --cached` by hand.
The other session committed as `dafc06b` moments later with its work intact, but only
because the reconstruction happened to be correct. A plain `git add` would have
committed the stub as a 588,614-line deletion of a file they meant to keep on disk.

Nothing about the situation required touching their index at all. The correct move,
available from the start, was:

```bash
git commit -F <msg> -- <your paths>     # commits those paths; index untouched
```

**Second worked example, same day -- the exception in practice.** Later that day a
publish preflight refused on 37 dirty files across four plugins. They looked like
another session's in-flight refactor. They were not: `git diff HEAD` was empty (the
working tree was byte-identical to HEAD), while the index held a pre-HEAD snapshot --
`plugins/unreal-kit/.claude-plugin/plugin.json` staged at `0.11.4` against HEAD's
`0.11.5`, and docstrings staged at wording HEAD had already superseded. Committing that
index would have downgraded a published plugin version and reverted 37 files. Nothing
was staged as a deletion. The index was discarded and the tree went clean. Note the
limit of that check: the empty-`git diff HEAD` and no-deletion clauses were verified
across all 37 paths mechanically, but "superseded by HEAD" was confirmed by reading two
files and generalized to the rest. The clause is only as strong as the sample -- read
enough of the staged diff to be sure, and say what you actually checked. The first example and this one differ on exactly one axis: whether the
index was the only record of the decision.

If you have already unstaged something that was not yours, say so plainly rather than
quietly reconstructing it -- the other session can restate its intent in one line, and
you cannot read it out of the index. That disclosure is required even when the exception
above applied, because "the checks passed" is a claim the other session may want to
test.

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

### The plugin-opinion razor

**The vision: the default is awesome and opinionated. Configurability is earned, not
assumed.** These plugins exist to expose powerful customizations that let a user produce
their best experience -- but an option nobody needs is a worse default plus a maintenance
burden. So a plugin holds its opinions confidently, and a setting appears only when the
opinion demonstrably costs a real user something.

The test that earns a config seam:

> **Can I articulate ONE SERIOUS, or TWO DISTINCT, user-preference scenarios in which this
> not being configurable leaves the user needing or wanting to uninstall the plugin, or to
> take remedial action against the default?**

The scenarios must be grounded in **realistic preferences of Claude Code power users** --
this marketplace's actual audience. Not hypothetical teams, not "someone might". A scenario
you cannot picture a power user actually having does not count, and neither does one whose
remedy is a single self-explaining error message.

If the test PASSES, the opinion must become configurable, with the opinionated default
preserved so nothing changes for everyone else. If it FAILS, leave it hardcoded -- that is
the correct outcome, not a deferred TODO.

The remediation for a passing test is always a configuration seam -- never prose telling the
reader to tolerate the default. When THIS repo cannot live with one of its own plugins'
opinions, that is a scenario, already evidenced: documenting the resulting warnings as noise
fixes one machine and converges nobody.

Criteria (OP-1..OP-7), how to detect each, worked examples of the test both passing and
failing, the findings table with per-finding verdicts, and the audit procedure:
[docs/reference/plugin-opinion-razor.md](docs/reference/plugin-opinion-razor.md).

#### The register -- opinions that PASS the test but we decline to configure anyway

Rare by construction. An entry here concedes that real users will want this changed, and
states why we refuse regardless, plus what they should do instead. An unregistered,
unconfigurable opinion whose test passes is a finding.

- **bootstrap owns dependency provisioning.** Manifests are the single source of truth;
  there is no supported path for a consumer to hand-install into a plugin venv. A team that
  wants manual control should not enable the plugin -- partial adoption produces a machine
  whose bootstrap is permanently wrong.
- **skills-kit's Architectural rule tier is not configurable.** The type contracts are what
  make an audit comparable across projects; a project that disables them is not running the
  same audit. Optional-tier rules and thresholds ARE configurable, and
  `references/configuring-standards.md` documents the boundary. A team disagreeing with a
  contract adds its own criteria via an additive standards file.
- **Code review renders to chat and is never persisted.** git-kit and p4-kit scope
  themselves to a conversational review; a team needing PR/Swarm comments or a CI artifact
  wants a different tool, and both SKILL.md scope blocks say so rather than assuming it
  silently.

An opinion that FAILS the test needs neither a register entry nor a seam -- it is simply a
good default. Opinions that PASS and are still unconfigurable are findings, tracked with
per-finding verdicts in the reference above; today those are the task system's durability
roots and git privilege, and the code-review reviewer roster and model tiers.

**Submit gate:** Apply the plugin-opinion razor to every workflow opinion this change adds or hardcodes -- for each, either name the config key and its default, or state the scenarios you tried and why the test fails.
Applies to:
- plugins/

`plugins/` is where plugin development happens, and everything under it ships to other
developers. The razor only works if it is applied per opinion at submit time, while the
change is still cheap to reshape -- once an opinionated default is published, teams have
built around it. One line per opinion discharges this, and "no new opinions" is a valid and
common answer. Criteria and audit procedure:
[docs/reference/plugin-opinion-razor.md](docs/reference/plugin-opinion-razor.md).

**A published plugin ships to other developers -- keep this repo's build machinery out of it.** Everything under `plugins/<name>/` is copied into a consumer's plugin cache, so a file that only makes sense inside plugins-kit is noise at best and misleading at worst: a generated fingerprint or baseline whose header names a `scripts/` tool the consumer does not have, a design doc recording our derivation rounds and remaining work, or generator plumbing embedded in a reference a consumer reads for guidance. Before adding content to a shipped plugin, ask **who reads this on a machine that is not ours** -- if the honest answer is "nobody", it belongs in the repo (`docs/`, `scripts/`, or a task folder), not in the plugin. The trap is incremental: maintainer material rarely arrives as a new file, it accretes inside a reference that already ships, so a file can double in size without anyone deciding to publish the additions. Watch for it particularly when a build step colocates its inputs with the artifact for convenience -- that convenience is a publishing decision.

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
   - Declare it on **every** plugin **except** bootstrap itself — whether or not the plugin ships a `bootstrap.json`. The edge is universal by design, so anything built on "bootstrap is present wherever a plugin is" holds without a per-plugin check; the fleet-wide user posture bootstrap owns ([docs/reference/first-run-experience.md](docs/reference/first-run-experience.md)) is the load-bearing case. The former carve-out for `bootstrap.json`-less plugins is **retired** — `agent-glue`, its only occupant, now declares the edge like everything else. Enforced at pre-commit by `scripts/check_bootstrap_dependency.py` (chained from `pre-commit-version-check.sh`; spec mirrored in `tests/repo-scripts/test_bootstrap_dependency.py`) and again, unbypassably, in `publish.py`'s preflight — the hook can be skipped with `--no-verify`, a publish cannot.
   - It belongs in **both** `plugin.json` and the generated marketplace entry; `scripts/regen_marketplace.py` propagates it automatically. A `dependencies` edit is a manifest change: it needs a version bump to reach consumers (same rule as any `plugin.json`/`bootstrap.json` edit).

2. **Runtime guard (provision-time).** A declared dependency guarantees bootstrap is *installed*, not that it has *run* — on first install bootstrap provisions each plugin's venv at the next SessionStart (and the cooldown can defer it). For that "installed-but-not-yet-provisioned" window, plugins that would otherwise crash with a raw `ModuleNotFoundError`/missing-interpreter error use the vendored **`bootstrap_guard.py`** (canonical: `plugins/bootstrap/bootstrap_lib/bootstrap_guard.py`). It is **stdlib-only** and **must never import `bootstrap_lib`** (that's the thing that may be missing); it detects absence via the per-plugin `~/.claude/plugins/data/<marketplace>/<plugin>/bootstrap.log` and exits with one actionable "install/enable plugins-kit:bootstrap" message instead of a raw traceback. It is **vendored** per plugin (copied next to the entry script and imported as a plain module), exactly like `path_repair.py`, with a drift test asserting copies match the canonical.

### Anti-pattern: hand-creating an artifact a plugin is supposed to produce

The sibling of [repairing a wedged machine by hand](#anti-pattern-repairing-a-wedged-machine-by-hand), one level up from bootstrap: that section is about not hand-fixing a *machine*, this one is about not hand-making an *output*. Same root error -- doing by hand the thing whose whole point was that the mechanism does it.

**When a plugin owns a workflow that produces an artifact -- a generated stub, an index, a config file, a report -- do not create that artifact yourself.** Build or fix the producing action, publish it, install it, and RUN it to make the artifact.

Producing it by hand proves nothing about the workflow, and the workflow was the deliverable. A hand-placed file cannot distinguish "the action works" from "the action is broken and I covered for it" -- and it looks like success in exactly the way that stops anyone looking further. The artifact existing was never the goal; the artifact existing *because the plugin made it* was.

The failure is tempting for an honest reason: the real path costs an edit, a version bump, a publish, a restart to pick it up, and only then a run. That round trip is the cost of shipping, not an obstacle to route around. Publishing is gated on the user (see "Committing and pushing to `dev` is unrestricted -- only PUBLISHES are gated"), so when the workflow is not yet published, **say so and wait** rather than substituting a hand-made result to keep moving.

If a *prerequisite* of the workflow is missing -- a source file it reads, a credential, an editor build -- report the missing prerequisite. Do not satisfy it by producing the final output directly; that discards the signal that the prerequisite was missing at all.

Corollary for verification, and the part most often skipped: **a plugin change is not verified by re-reading the diff.** It is verified by the published plugin doing the thing on a machine that installed it. Until then the correct status is "written", not "working" -- and a background agent's report of what it intended is not evidence either.

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

- A git-ignored task root is a SUPPORTED configuration as of awesome-kit
  0.26.0, not a misconfiguration. Do not read such a folder as "uncommitted
  work" -- it is scratch by design. Mechanics: `Skill(awesome-kit:task)`, the
  GIT-IGNORED task root disposition. Before 0.26.0 `archive` here crashed
  mid-write at `git add`; if you see that, the installed plugin predates the
  fix.
- **The corollary is sharper here than anywhere else: the folder is the only
  copy.** Removing it is unrecoverable -- no commit, no reflog, nothing to
  restore from. Relocate anything that must outlive the task to the repo it
  describes and declare it with `update --durable-output` BEFORE archiving,
  which is what the `durable_outputs` rule already requires.
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
    - id: never_hand_make_a_plugins_output
      keywords: [hand-create artifact, hand-place file, copy the file myself, plugin should generate it, refresh action, generated stub, index, report, prove the workflow, publish and run, skip the round trip, missing prerequisite, written not working, verify by running]
      summary: Never hand-create an artifact a plugin's workflow is supposed to produce. Build or fix the producing action, publish it, install it, and run it -- a hand-placed file cannot distinguish a working workflow from a broken one.
      detail: |
        Sibling of never_hand_repair_a_wedge, one level up: that one is about not
        hand-fixing a MACHINE, this one about not hand-making an OUTPUT. When a plugin
        owns a workflow that produces something -- a generated stub, an index, a config,
        a report -- producing it yourself proves nothing about the workflow, and the
        workflow was the deliverable. It also looks like success in the way that stops
        anyone looking further.
        The temptation is honest: the real path costs an edit, a version bump, a
        publish, a restart to pick it up, then a run. That is the cost of shipping, not
        an obstacle. Publishes are gated on the user, so when the action is not yet
        published, SAY SO AND WAIT rather than substituting a hand-made result.
        If a PREREQUISITE is missing (a source file, a credential, an editor build),
        report the prerequisite -- satisfying it by producing the final output directly
        discards the signal that it was missing.
        Corollary: a plugin change is not verified by re-reading the diff, nor by a
        background agent's report of what it intended. It is verified by the published
        plugin doing the thing on a machine that installed it. Until then its status is
        "written", not "working".
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
        Criteria OP-1..OP-7, detection methods, examples both ways, the table of known
        unremediated findings, and the audit procedure:
        docs/reference/plugin-opinion-razor.md. OP-1 (no maintainer-only material on the
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
        The mandatory `git diff --staged` check surfaces foreign files, and the rule that
        the staged set must be exactly your files makes unstaging them FEEL like
        compliance. It is not: that rule scopes your COMMIT, not the index. The index is
        shared mutable state with no reflog, and it does not record intent -- a deliberate
        `git rm --cached` (untracking a generated file, usually paired with a `.gitignore`
        change staged alongside it) is indistinguishable from an accidental `git add` of a
        deletion. Restore one as the other and you silently corrupt the other session's
        commit.
        `git commit -F <msg> -- <paths>` commits the working-tree state of exactly those
        paths and leaves the index otherwise intact, which removes the only reason anyone
        would reach for `git reset` here. Reserve `git reset` for an index you own. If you
        have already unstaged something that was not yours, SAY SO rather than
        reconstructing it silently -- the owning session can restate its intent in one
        line and you cannot read it out of the index.
        The exception: discard a staged state only when ALL of `git diff HEAD` is empty
        for those paths, the staged content is demonstrably superseded by HEAD (a version
        going backwards, text HEAD already carries a newer revision of), and nothing is
        staged as a deletion or untrack. Then the index is a stale re-add whose content is
        already in history. The operative question behind both the rule and its exception
        is whether the index holds information that exists nowhere else.
        Full narrative and both worked examples: "Anti-pattern: unstaging another session's
        work to scope your own commit" in Development Workflow.
      origin: "2026-08-08 -- a unreal-kit stub staged as 588,614 deletions was `git restore --staged`-ed to scope an orchestrate commit; it was a deliberate `git rm --cached` paired with a staged .gitignore change, and putting it back required inferring that intent by hand."
      added: "2026-08-08"
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
    - id: orchestration_yaml_is_generated
      keywords: [orchestration.yaml, generated decision half, tier-principles.md, generate_orchestration.py, hand-edit orchestration.yaml, one-way authorship, orchestrate skill policy]
      summary: The decision half of awesome-kit's orchestrate skill policy (plugins/awesome-kit/skills/orchestrate/defaults/orchestration.yaml) is GENERATED from maintainer-only principles -- never hand-edit it or back-fill a principle to match a hand edit.
      detail: |
        Full chain, the one-way authorship rule, why the generator's two inputs
        (tier-principles.md, lexicon.md) live in two different roots on purpose,
        and the reversion posture: docs/reference/orchestrate/CLAUDE.md. That
        file is the disclosure of record because the SKILL.md links that used to
        point at this machinery were deliberately removed when the maintainer-only
        derivation source was unshipped from the published skill (commits
        1ba87e2, 0e08fca, 8bc3f9c, d6d7aae, 566c941) -- a consumer's plugin
        install has no docs/ directory for those links to resolve against.
      origin: "2026-08-08 -- disclosure gap identified after the unshipping move removed the in-plugin links that used to name this chain."
      added: "2026-08-08"
  conventions:
    - rule: Commit and push to dev freely without asking; only a PUBLISH (dev -> master) needs the user. Do not coordinate around other agent sessions' concurrent work.
      keywords: [commit freely, push freely, no permission, dev branch, only publishes gated, other agents, concurrent sessions, shared tree, git commit -- paths]
      why: Nothing on dev reaches a consumer -- master is the cache source -- so a commit or push is reversible working-branch state, while a publish broadcasts to every machine. Scope commits by path for readability, and use `git commit -F <msg> -- <paths>` when the index holds another session's staged work. See "Committing and pushing to dev is unrestricted" in Development Workflow.
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
