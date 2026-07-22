# Investigation findings (2026-07-22): repo-sync anatomy, history, seam

Condensed from a three-agent read-only investigation across claude-settings
(`~/.claude`), plugins-kit (this repo), and env-config
(`$DEVROOT/env-config`). This is the design input for absorbing repo-sync
into bootstrap. Line numbers were verified on 2026-07-22 working trees and
will drift.

## What repo-sync is today

A user-side contract script, `~/.claude/scripts/env/repo-sync.sh`
(~527 lines with the then-uncommitted nag-razor rewrite), bound by the
`repo-sync` entry in `~/.claude/env.json` (`check`/`fix` opaque shell
strings + agent_instructions) and run by bootstrap's env_checks engine in
the gated SessionStart env pass.

- `check`: read-only. Exit 0 iff every applicable `auto` repo is in a state
  the pass neither needs to converge nor report.
- `fix`: clone missing `auto` repos on their declared branch; for present
  CLEAN on-branch repos, fetch then `pull --ff-only` when behind, push when
  ahead. Ends by re-running the check evaluation.
- Refuses, always: dirty trees (never fetched/pulled/pushed), wrong branch
  (never auto-switched), diverged histories (no merge/rebase ever),
  interrupted merge/rebase/cherry-pick/bisect, `report`/`skip` repos.
- Nag razor (the rewrite): report only states that are (1) not
  self-evident and (2) won't clear themselves. Age-gated: dirty idle > 24h,
  diverged > 24h, detached HEAD holding unreferenced commits > 24h, stuck
  operations > 4h. Ahead/behind (clean) fail check silently just to drive
  fix. Wrong branch and stashes: never reported. The repo the session's cwd
  is in is exempt from all age-gated nags.
- Preflight hard errors: missing `$DEVROOT` (no fallback -- the old `~/Dev`
  fallback cloned three repos into the wrong root on 2060W), missing
  yq/jq, missing/empty registry (never a silent green pass).
- Data: `~/.claude/config/repositories.yaml` (name, path with $DEVROOT/~
  expansion, github_url, branch, optional update_script, sync policy
  auto|report|skip; claude-settings itself is `report` -- the live config
  repo is never auto-pushed) + env.json `machines.<host>.skip_repos`.
- Output: `repo-sync:`-prefixed lines; a joined summary line exists
  specifically because the engine surfaces only the last non-empty line.

## What the engine contributes (and only this)

- SessionStart shim -> background engine; env pass gated by an
  env_state.json stamp (manifest hash + engine version + last result).
- env_checks dispatch: opaque shell strings via Git Bash on Windows,
  default 600s timeout, fix exit code advisory, re-run check authoritative
  ("no trust exceptions"), `agent_instructions` appended to the failure's
  agent-facing message (added 2026-07-16, 0.48.0, with repo-sync as the
  motivating consumer -- but landed generic).
- Surfacing: last non-empty output line becomes the detail; failures reach
  the user as one loud systemMessage block on next prompt
  (UserPromptSubmit), Claude via additionalContext.
- The engine knows nothing about repositories. Its own git code is a
  different concern: `git_deps` = pinned/sparse read-only tool deps,
  explicitly "clone once" in steady state (existing clone on right branch
  is never pulled; pull is remediation only; pinned commits re-checked-out
  on pin change) -- engine.py around line 3030. Marketplace clone
  lifecycle = plugin distribution fetch/pull. Neither pushes, neither has
  dirty-tree policy, neither reads repositories.yaml.

## History: designed, not drifted

- Sync machinery originated in env-config (2025-10-22, `062907f`: registry
  + an 827-line check_runner with clone/pull/push).
- 2026-07-05 `ae2822f` (env-config): "repo-sync ruling ratified -- moves to
  env.json as contract script".
- 2026-07-06: env-config `0145795` deletes the machinery; claude-settings
  `48626dfb` lands script + registry + env_check entry ("doctrine flip":
  claude-settings is the fleet root, env-config downstream, arriving via
  repo-sync). Engine's env_checks contract landed the same week (E1
  series, 0.33.0-0.35.0).
- No file named repo-sync ever existed in plugins-kit (git log --all
  empty); dev and master identical at time of investigation.
- The plugin docs codify the seam (manifest-reference.md, env_checks
  section): contract scripts live in claude-settings under scripts/env/,
  invoked ~-anchored, "the engine never tries to locate it"; env_checks is
  the escape hatch for what the declarative features do not model, with
  repo-sync named as an example.

## Why absorption is justified (assessment)

- The current arrangement's biggest liability is untested heuristics in a
  repo with no test convention; it has bitten twice (wrong-root clone,
  CRLF skip_repos). plugins-kit mandates tests; the env_checks engine
  itself has 170+.
- The one-line output channel is only fixable engine-side; the entire nag
  razor is the script compensating for a binary silent-ok/loud-fail
  channel.
- The "marketplace audience" cost of a plugin feature is near zero
  (single-user marketplace); the real cost is version-bump + publish per
  policy tweak -- which is why the gate: absorb after the policy stops
  churning.
- Principle to preserve (already stated in plugin docs): personal fleet
  content rides in the user layer. Mechanism to the engine, policy data
  stays in `~/.claude`.

## env-config loose ends (cutover scope)

- `--sync` = literally `subprocess.run(["bash", ~/.claude/.../repo-sync.sh,
  "fix"])`; uses bare `bash` (not its Git Bash resolver) -- delete rather
  than fix.
- `--update-repos` runs per-repo `update_script`s from repositories.yaml;
  ratified as excluded from the env pass; needs a new home decision, not
  necessarily bootstrap.
- `repo_root.py` comments still claim a `~/Dev` fallback "matching
  repo-sync.sh" that the script removed on 2026-07-15 -- already-diverged
  coupling-by-comment.
- `~/.claude/docs/fresh-machine.md` is stale (lists dropped
  kitaekatt-plugins repo; describes pre-rewrite dirty-tree behavior).
- Known small defects at time of writing: duplicated header fragment in
  the script's working copy (~lines 53-54).
