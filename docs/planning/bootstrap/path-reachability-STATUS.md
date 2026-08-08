# PATH-reachability work — STATUS / resume doc

**Date:** 2026-05-30 (updated 2026-05-31)
**Branch:** dev
**Status:** COMMITTED + PUSHED to origin/dev as part of a coordinated 3-agent batch
(`0e266be`). NOT yet published to master. Smoke test PASSED.
**Design doc (companion):** `docs/planning/bootstrap/path-reachability-check.md`

---

## PUBLISH-RESUME CHECKLIST (next session — start here)

The batch (PATH-reachability + shared-libs + skills-kit cohesion) is committed to
`origin/dev` @ `0e266be` and smoke-tested green via `claude-dev`. Master is still at
`origin/master @ 14380f8` — nothing has reached consumers. Remaining work, in order:

0. **Confirm dev-tree restored.** `python scripts/dev-tree.py status` must say
   normal/cache. If it says DEV-TREE (a `claude-dev` exit trap was skipped), run
   `python scripts/dev-tree.py normal`. (Everyday `claude` loads from cache only in
   normal mode.)

1. **Wait for the cohesion agent.** They wanted to do more skills-kit work before
   publish (not started as of 2026-05-31). When done, get from them:
   (a) whether they re-bumped skills-kit (0.15.0 -> ?), so re-run
   `python scripts/regen_marketplace.py` if so; (b) confirmation their tree is
   commit-ready + audits clean. Their new commits land on dev and get picked up by
   the master-assembly step naturally.

2. **Master merge = THE publish moment (needs explicit user go-signal).**
   - DO NOT fast-forward dev -> master: `git log origin/master..origin/dev` shows
     ~64 commits incl. agent-glue + other unrelated WIP (CLAUDE.md gotcha 1).
   - Assemble on `origin/master @ 14380f8`. Take **dev's** `shared_lib.py`
     (executable-prepend `.pth`, the shadow-bug fix) — NOT master's 14380f8 copy.
   - Batch versions (all triads aligned, marketplace regenerated, drift-check green):
     bootstrap 0.14.0 · git-kit 0.3.0 · p4-kit 0.12.0 · openrouter-kit 0.2.0 ·
     skills-kit 0.15.0 (or cohesion agent's re-bump) · unreal-kit 0.9.16 ·
     awesome-kit 0.6.6. workflow-kit/agent-glue stay dev-only (published:false).
   - bootstrap 0.14.0 / unreal-kit 0.9.16 deliberately clear the shared-libs agent's
     already-live origin/master numbers (0.13.0 / 0.9.15) to avoid cache collisions.

3. **update06 close-the-loop (post-merge, same session).** bootstrap pyproject went
   to 0.14.0 -> re-lock update06: `cd ~/Dev/update06/plugins/update &&
   uv lock --upgrade-package bootstrap`, then bump update06's own plugin.json +
   marketplace.json version, commit, push. (Prior 1.0.36 lock points at the stale
   14380f8.) Per CLAUDE.md "always close the loop" — do not defer.

4. **Optional cleanup (unrelated to publish).** `~/.bashrc` has 4 cruft PATH lines
   from this work: `export PATH="/from/user:$PATH"`, `/from/project`, `/from/local`
   (junk from a path-entry test run), and a duplicate draw.io line (both
   `/c/Program Files/draw.io` and `C:/Program Files/draw.io` — keep one). Show the
   user a diff before editing their `.bashrc`.

### Smoke-test result (2026-05-31, claude-dev / dev-tree mode) — PASS
- bootstrap 0.14.0 loaded + ran from disk; no fix-all, no SessionStart errors.
- PATH-reachability: draw.io resolves (`passed=True, on_path=False`); the
  `draw.io: FAILED` lines in bootstrap.log are STALE (2026-05-30, old 0.10.11
  engine) — this session's 0.14.0 blocks have zero draw.io lines.
- shared-libs: git-kit + p4-kit venvs `import bootstrap_lib` via `.pth` OK;
  standalone `import openrouter_kit` via `.pth` OK.
- Not tested (out of scope): workflow-kit->openrouter_kit->openai chain (workflow-kit
  is dev-only, venv not provisioned); covered by static test_dependency_completeness.py.

---

## TL;DR

Implemented "option (b)": made PATH-reachability a first-class part of a bootstrap
tool check, so a tool that exists on disk but isn't on PATH is treated as
actionable (auto-linked onto PATH) instead of either false-passing or
false-failing. Triggered by the draw.io case: `winget` exited 43 ("already
installed") + the binary's dir wasn't on PATH -> engine logged a false
`install command failed`.

**Status: implemented + unit-tested (63 targeted tests green). NOT committed.**
The work sits uncommitted in the working tree alongside UNRELATED in-flight WIP
(a separate "shared-libs" feature -- see "Do not touch" below). Per CLAUDE.md
gotcha 2, these must not be swept together.

---

## What was changed (MINE -- the PATH-reachability feature)

| File | Change |
|---|---|
| `plugins/bootstrap/bootstrap_lib/tool_check.py` | `installPath` accepts str OR list; `$VAR`/`${VAR}` expansion (os.path.expandvars); new optional `check` command field (exit 0 = present); `CheckResult.on_path` field; resolution order = installPath candidates -> check cmd -> shutil.which. `run_install` docstring notes exit code is advisory. |
| `plugins/bootstrap/bootstrap_lib/engine.py` | Extracted the two near-identical tool loops into shared `_process_tool_entry()` + `_link_tool_dir_to_path()` (refactor, per user's choice). Both `_process_self_setup` (~line 738) and `_process_manifest` (~line 1327) now call the helper. New behavior: (a) found-but-off-PATH tool -> auto-add its dir via `add_path_to_shell_config` + live `os.environ["PATH"]`, log action line; (b) after ANY install attempt, re-check regardless of installer exit code (fixes winget-43 false failure); only record failure if re-check still misses. |
| `plugins/bootstrap/.claude-plugin/plugin.json` | version 0.12.1 -> 0.13.0 |
| `plugins/bootstrap/pyproject.toml` | version 0.12.0 -> 0.13.0 |
| `plugins/bootstrap/skills/bootstrap/references/manifest-reference.md` | Rewrote "Tool installPath" section -> "Tool resolution: installPath, check, PATH linkage" (list form, $VAR, check cmd, tool->PATH auto-link, install-exit-codes-advisory). |
| `plugins/bootstrap/skills/bootstrap/references/dependency-philosophy.md` | Added two bullets under "Implications for manifest authors": tool->PATH linkage (owning the chain, P4) + install-exit-codes-advisory. |
| `tests/bootstrap/test_tool_check.py` | Added `TestInstallPathList`, `TestCheckCommand`, `TestOnPath` classes. |
| `tests/bootstrap/test_tool_path_linkage.py` (NEW) | `TestLinkToolDirToPath` + `TestProcessToolEntry` -- off-PATH linkage, winget-exit-43 (install nonzero but recheck passes), install_failed, installed_but_path_stale. |
| `docs/planning/bootstrap/path-reachability-check.md` (NEW) | The design doc. |
| `~/.claude/bootstrap.json` (user config, NOT in repo) | draw.io tool entry: replaced dead `check` field with `installPath: "C:/Program Files/draw.io"` (Windows form -- `/c/...` MSYS paths do NOT work with the engine's `os.path.isfile`). Also `path_entries` uses `C:/Program Files/draw.io`. |

### Verification done
- 63 targeted tests pass: `uv run --extra dev pytest tests/bootstrap/test_tool_check.py tests/bootstrap/test_tool_path_linkage.py tests/bootstrap/test_path_check.py tests/bootstrap/test_tool_paths.py -q`
- draw.io resolves correctly: `check_tool('draw.io', install_path='C:/Program Files/draw.io')` -> `passed=True, on_path=False, path='C:/Program Files/draw.io\draw.io.exe'` -> engine will auto-link the dir to PATH.
- draw.io desktop 30.0.4 IS installed at `C:\Program Files\draw.io\draw.io.exe` (winget; predated this work). The CLI works (`draw.io.exe --version` -> 30.0.4).

---

## KNOWN ISSUE: 8 pre-existing suite failures (NOT caused by this work)

Full `pytest tests/bootstrap/` shows 8 failed, 602 passed, 2 skipped, 2 xpassed.
PROVEN pre-existing: stashing my `engine.py` + `tool_check.py` (revert to HEAD) and
re-running the failing tests reproduces them IDENTICALLY. Two root causes, both
environmental:

1. Non-hermetic engine tests (`test_engine.py::test_first_run_silent_on_success`,
   `test_cached_run_silent`, `test_engine_background.py` x3, `test_engine_multiplugin.py::test_no_enabled_plugins_emits_log`):
   they run the real engine which reads the real `~/.claude/bootstrap.json` +
   installed plugins, so they see real plugins (awesome-kit, claude-ui-kit,
   private-marketplace plugins) and the draw.io fix-all leaking into "should be
   silent" assertions.
   Would pass in clean CI; leak on this configured machine.
2. bash `source` round-trip (`test_engine_multiplugin.py::test_plugin_venv_exports_env_var_via_claude_env_file`,
   `test_venv_check.py::test_path_with_spaces_is_shell_quoted`): "Failed to attach
   disk ... ERROR_PATH_NOT_FOUND" -- a WSL/sandbox bash issue in this session, not code.

-> A clean full-suite green is NOT achievable on this machine right now, independent
of this change.

---

## DO NOT TOUCH (unrelated in-flight WIP, was here before this work)

A separate "cross-plugin shared libraries" feature is uncommitted in the same
working tree. NOT mine -- leave alone, do not stage with the PATH work:
- `plugins/bootstrap/bootstrap.json` (`shared_libs` block)
- `plugins/bootstrap/bootstrap_lib/manifest_merge.py`
- `plugins/bootstrap/bootstrap_lib/shared_lib.py` (untracked)
- `plugins/bootstrap/skills/bootstrap/references/engine-internals.md` (shared-libs section)
- `tests/bootstrap/test_dependency_completeness.py`, `tests/bootstrap/test_shared_lib.py` (untracked)
- Plus broader WIP across `plugins/workflow-kit/`, `plugins/openrouter-kit/`, `plugins/skills-kit/`, CLAUDE.md, and 4 git stashes (`skills-kit 0.5.0`, `parallel cohesion-first`, `pre-bucket-b unrelated bootstrap engine.py diff`, `p4-kit custom_bootstrap`).

`git status --short -- plugins/bootstrap docs/planning/bootstrap tests/bootstrap`
shows MY files interleaved with the shared-libs files. When committing, stage ONLY
the MINE files above (use `git add -p` / explicit paths, never `git add .`).

---

## TO RESUME / REMAINING WORK

1. Decide commit scope. Stage only the MINE files (per table). Confirm with
   `git diff --staged` that no shared-libs / workflow-kit / skills-kit hunks leak in.
2. Pre-publish smoke test (REQUIRED for manifest changes). This touches
   `bootstrap.json` schema semantics (`check`, list `installPath`) -> per CLAUDE.md
   the validator is `claude-dev` (dev-tree mode), NOT just `claudx`. Agent cannot
   self-run it; user runs `claude-dev`, exercises a tool with off-PATH
   `installPath`, confirms the auto-link + no false failure.
3. Publish flow (3 steps, only on user go-signal): version already bumped to
   0.13.0 in plugin.json + pyproject.toml. Need to: regen marketplace
   (`python scripts/regen_marketplace.py` -- currently still shows 0.12.1 because
   the bump isn't committed/regenerated yet), push dev, merge to master.
4. update06 (downstream consumer). bootstrap pyproject version bumped -> after
   publishing, bump update06's lockfile (`cd ~/Dev/update06/plugins/update &&
   uv lock --upgrade-package bootstrap`) + update06's own version, per CLAUDE.md
   "update06 -- the bootstrap bootstrapper". Close the loop same session as publish.
5. Optional follow-up: the 8 non-hermetic tests are a separate latent problem
   worth fixing (make engine tests isolate from real `~/.claude`), but that's NOT
   part of this feature.

---

## Side context (this session, unrelated to the engine change)

- Diagram-skills benchmark lives in `tmp/diagram-experiments/` and was published to
  https://kitaekatt.github.io/pastebin/diagrams/ (Model 1, page-per-diagram).
- Four diagram skills installed at `~/.claude/skills/`: archify (pre-existing),
  diagram-design, drawio-skill, hand-drawn-diagrams.
- `~/.claude/bootstrap.json` cooldown was reset for /d/Dev/plugins-kit so a fresh
  session re-runs bootstrap (which is how draw.io's install/resolve was exercised).
