# Remediation Reference

Detailed check methods and remediation actions for all condition categories the bootstrap engine can handle. For a summary table, see the SKILL.md "Remediable Condition Categories" section.

## Two outcomes: auto-fix or ask

Every issue bootstrap surfaces resolves to **exactly one of two outcomes** — there is no third "guide the user through it / work through it with Claude" path.

- **AUTO — fix it now, no prompt.** This is the **default**, because bootstrap manages a fleet. Claude fixes it immediately: runs the install/clone/merge command, edits the manifest, whatever the remediation is. **Installing software is AUTO** — bootstrap will install non-elevated packages unattended without asking. Do not wait for the user to say "fix-all".

- **ASK — get the user's go-ahead via the `AskUserQuestion` tool first.** Only when the fix needs one of exactly three things the user alone can provide:
  - **`elevation`** — admin / root / UAC / `sudo` that a background hook cannot obtain.
  - **`action`** — a physical or out-of-band act only the user can perform: press a device's button (e.g. a Hue bridge link button), restart the IDE, install a GUI app that has no unattended installer.
  - **`info`** — a value only the user holds: an API key or secret, which machine in the fleet this is.

  The `AskUserQuestion` prompt is always a single question with exactly two options, "Do nothing" leading (an absent-minded Enter changes nothing; bootstrap re-checks next session) and "Fix" second. Claude acts only on "Fix", and never re-prompts.

**How the outcome is decided:** `engine._ask_reason(failure)` returns `elevation` / `action` / `info` (→ ASK) or `None` (→ AUTO). An explicit `ask_reason` on the failure wins — that is how a check or a plugin `custom_bootstrap` (via `ctx.add_failure(..., ask_reason="action")`) declares it needs the user. Otherwise the reason is derived from signals the engine already records (`install_state == "needs_elevation"`, `type in {python_stub, elevation_script}`, `manual_install`, `bootstrap_outdated`, `config`, ...). **Anything not marked is AUTO.**

**Authoring a `custom_bootstrap` failure:** if the fix is runnable without the user, give it a remediation and leave it AUTO. If it needs the user, set `ask_reason` to the matching category and a friendly `user_msg` (the plain-language line the user sees, e.g. "hue-kit wants to pair with your Hue bridge") plus an `agent_msg` describing the post-consent steps.

## Configuration Conditions

| Condition | Check Method | Remediation |
|-----------|-------------|-------------|
| Directory not in PATH | Read shell RC files or query OS environment variable | Modify persistent PATH configuration (platform-specific) |
| JSON file lacks expected entries | Compare reference entries against target file | Merge missing entries into target JSON |
| Application config setting not enabled | Read config/ini file for setting value | Write setting to config/ini file |

## Tool Conditions

| Condition | Check Method | Remediation |
|-----------|-------------|-------------|
| CLI tool not installed | `shutil.which(name)` | Run platform-specific install command -> re-check -> escalate to fix-all only if still missing |

## Library / Data Conditions

| Condition | Check Method | Remediation |
|-----------|-------------|-------------|
| Python venv missing or broken | Check dir -> binary -> interpreter runs -> packages importable | `uv sync` from `pyproject.toml` |
| Project venv missing or broken | Same checks against `<project_dir>/.venv` | `uv sync --project <project_dir> [--extra ...]` |
| PyPI package missing | Check extracted file exists locally | Download from PyPI and extract |
| Git dependency not cloned / wrong branch / wrong pinned commit | Check dir exists + is a git repo + `rev-parse` matches the declared branch (or pinned `commit`) | Clone once; pinned commits are fetched + re-checked-out. No steady-state pull — an existing clone on the right branch is never updated against its remote |

## Marketplace Conditions

| Condition | Check Method | Remediation |
|-----------|-------------|-------------|
| Marketplace not registered | Check `known_marketplaces.json` for `installLocation` | `claude plugin marketplace add <url>` |
| Marketplace stale (`alwaysUpdate`) | Always (no check — unconditional on every session) | `claude plugin marketplace update <name>` |
| Marketplace pinned but clone at wrong commit | Resolve `pin` via `git rev-parse` (with `git fetch` + retry on a miss); compare resolved SHA to clone HEAD | `git checkout --detach <sha>`; force `autoUpdate: false` in `known_marketplaces.json`; record pin + prior `autoUpdate` in `marketplace_pins.json` |
| Pin removed from manifest but marker recorded | Compare manifest `pin` fields against `marketplace_pins.json` | `git checkout <default branch>` (origin/HEAD, falling back to master/main probing), then the normal marketplace update; restore recorded `autoUpdate`; remove the marker entry |
| Pin unresolvable (bad SHA/tag, clone missing) | `git rev-parse` still fails after fetch, or `installLocation` dir absent | Fix-all failure with guidance: check the SHA/tag, register the marketplace, or remove the pin |

While a marketplace is pinned, `alwaysUpdate` is skipped (a one-line "alwaysUpdate ignored while pinned" warning is emitted instead). See the `pin` section in [manifest-reference.md](./manifest-reference.md) for full semantics.

## Plugin Conditions

| Condition | Check Method | Remediation |
|-----------|-------------|-------------|
| Plugin not installed | Check installed plugins registry | Install plugin at declared scope |
| Plugin installed but unwanted | Check installed plugins registry | Uninstall plugin |
| Plugin out of date | `git ls-remote` vs cached commit SHA | Update plugin at declared scope |
| Plugin at wrong scope | Compare installed scope vs declared `scope` in manifest | Uninstall from current scope, reinstall at declared scope |

## Manual Operations (Blocking Conditions)

These are the **ASK** category (see [Two outcomes](#two-outcomes-auto-fix-or-ask)): auto-configuration cannot complete without the user, because the fix needs elevation, a user action, or information only they have. Claude surfaces each via the `AskUserQuestion` tool (elevation items are batched into one aggregate offer). They are *not* a separate "manual, work-through-it" outcome — the only two outcomes are auto-fix and ask.

| Condition | Check Method | Remediation |
|-----------|-------------|-------------|
| Config information missing and can't be auto-detected | Check config file for required fields | Ask user for information, write to config file |
| External app requires config change and/or restart | Modification applied that requires restart | User restarts external application, types `fixed` |
| Claude Code requires config change and/or restart | Modification applied that requires restart | User restarts Claude Code |
| Operation needs elevation (sudo/UAC) in a non-interactive pass | Privilege probe (`sudo -n` / admin token) failed when the op ran | Deferred into ONE per-OS fix queue — `<data_dir>/elevate/queue.json` plus a `bootstrap-fix.{sh,bat}` launcher shim — surfaced as one aggregate `elevation_script` item. Windows: a `fix-all` re-run launches the fix runner itself (see below). Unix: user runs the shim; the next session's re-check clears it (nothing to confirm) |

### The fix queue and its runner

Deferred operations are serialized as **typed tasks** in
`<data_dir>/elevate/queue.json` — data, not generated shell text — and executed
by `bootstrap_lib/fix_runner.py` (`python fix_runner.py <queue.json>`), the one
place bootstrap has a TTY. The queue and its `bootstrap-fix.{sh,bat}` launcher
shim are rewritten every pass and deleted once nothing is deferred, so the offer
disappears when the operations succeed.

| Task kind | What the runner does |
|-----------|----------------------|
| `command` | `bash -c <command>`, `sudo`-wrapped on Unix when the task is `elevated` |
| `apt` | `apt-get update`, then one `apt-get install -y <all queued packages>` |
| `brew_installer` | Runs the official Homebrew installer. Never elevated — it refuses to run as root and elevates itself where it needs to |
| `secret` | Prompts with echo off, writes the value 0600 and user-owned. **Available but unwired** — no producer emits one yet |
| `path_prune` | Removes the task's `entries` from the Windows User PATH, backing the old value up to `entries`' sibling `path_backup.txt` first. **Never elevated** — `HKCU` is the user's own hive; it is queued for *consent*, not privilege, because it deletes things |

The runner **prints the plan** before executing anything — one numbered line per
task, quickest first, marked `admin` where elevated and flagged where it
downloads. A data file is more opaque than the shell script this replaced, whose
real virtue was that the user could read it before approving; the plan restores
that. The UAC (or sudo) prompt is the consent, the plan is the disclosure — and
for `path_prune` the queue file itself is part of that disclosure: it lists every
entry the prune will delete, verbatim, before the user agrees to anything.

**fix-all is "needs the user", not "needs admin".** `path_prune` is the case that
makes the distinction concrete: it requires no privilege at all, and rides the
queue purely because deleting PATH entries must be consented to rather than done
to someone by a background hook.

**Opportunistic tasks: worth fixing, never worth their own nag.** A `command` or
`path_prune` descriptor may carry `opportunistic: true` (a `FixTask.opportunistic`
flag, serialized into `queue.json`). Such a task rides the queue and executes
whenever the runner is launched for *other* work — but a queue containing **only**
opportunistic tasks surfaces nothing: no `elevation_script` aggregate, no
AskUserQuestion prompt, no fix-all launch, and the covered per-item failures are
dropped from the pass (`engine._elevation_step`, gated on
`fix_queue.has_actionable`). The queue and shim still stay on disk, so the work
piggybacks on the next real deferral or a by-hand shim run. The dead-PATH prune
is the built-in opportunistic task (housekeeping — valuable, not urgent); an
`env_checks` entry can opt in with `opportunistic: true` alongside `elevated`.

It **continues past a failed task** rather than aborting on the first one (the
old `.bat` aborted): the tasks are independent, and the engine's next re-check —
not the runner — is the authority on what actually cleared, so a task that fails
here simply stays failed there. Exit codes: `0` every task completed, `2` at
least one did not, `3` the queue is unreadable or names an unknown kind (a
version skew — failing loudly beats skipping an elevated task silently, which
would look like success to the re-check).

**Privilege is per task, not per run.** On Unix the runner runs **as the user**
and wraps only `elevated` tasks in `sudo`, so anything it creates stays the
user's and `$HOME` is already correct — the old script ran wholesale under `sudo`
(`HOME=/root`), which is why it had to rewrite `~`. On Windows the engine
launches the whole runner elevated in one UAC hop; UAC preserves the user
profile, so `elevated` is effectively advisory there.

**Tasks run with bash's own dir prepended to PATH.** An elevated process gets
the user's *default* environment (`Start-Process -Verb RunAs` does not inherit
the caller's), whose PATH has no Git `usr/bin`; `bash -c` is non-login, so msys
never adds `/usr/bin` either. Before this (0.49.0, observed live) every queued
command died with exit 127 — `ln: command not found`, `bash: command not
found` — while the runner itself launched fine off the queue's baked absolute
bash path. `fix_runner._child_env` prepends the bash binary's directory to the
task subprocess PATH, which is msys `usr/bin` itself.

**Everything the runner prints is tee'd to `<data_dir>/elevate/fix-runner.log`**
(overwritten per run), including child output — the pump in `fix_runner._run`
routes task stdout+stderr through the runner's own stdout precisely so the
transcript captures it. The elevated window closes on a keypress; the transcript
is the only account of a failure that survives it, and the `elevation_script`
failure message names it after a launch that did not complete. Secrets never
reach it (`getpass` bypasses stdout).

### Dead PATH entries: cached scan, uncached finding

`bootstrap_lib/path_prune.py` detects Windows User PATH entries whose directory
no longer exists. Nothing removes a PATH entry once added, and each dead entry is
textually unique so nothing collapses them either — they accumulate forever. A
bloated PATH is not cosmetic: it is what `path_repair.py` exists to survive
(cmd.exe silently truncates an oversized PATH during venv activation, leaving the
Python child unable to find its tools).

Scanning probes the filesystem per entry — potentially slow, potentially an
offline network share — so it is gated on a hash of the raw registry PATH. **What
is cached is the RESULT, not "did I already report this".** That distinction is
the whole design:

| Situation | Rescan? | Surfaces? |
|---|---|---|
| User declined; PATH unchanged | No (hash hit) | **Yes — every session** (into the queue; see below) |
| User pruned | Yes (hash miss) | No — result empty, self-clearing |
| Something added a dead entry | Yes (hash miss) | Yes, naming it |

Cache "already reported" instead and a declined prune is detected once and never
mentioned again. Caching the result means a skip costs the *scan*, never the
*finding*; and because a prune changes the PATH, the finding clears itself with no
"type fixed" ritual.

"Surfaces" here means the finding re-enters the fix queue each session. Whether
the *user* sees a nag is a separate, later gate: the prune descriptor is
**opportunistic** (see above), so it only appears in the fix-all offer when a
non-opportunistic task is queued alongside it — a prune-only queue logs one
verbose line and stays silent.

`scan()` returns `None` for **no verdict** (not Windows, or `BOOTSTRAP_SKIP_REGISTRY`
set) as distinct from `[]` for **ran and clean** — collapsing the two would let a
check that never ran report itself as one that passed.

**`BOOTSTRAP_SKIP_REGISTRY` suppresses the read, not just writes.** The registry
is global state that ignores the HOME isolation tests rely on, so a scan inside a
test reads the *developer's* PATH — every engine test then inherits whatever dead
junk that machine has.

**Deciding "dead" is the dangerous part**, and the two obvious ways are both
wrong. `os.path.isdir` **never raises** — it swallows `OSError` internally and
returns False — so an offline `\\nas\share` reads as dead. `os.stat` raises, but
on Windows an offline UNC host, an unmapped `Z:`, and a genuinely missing
directory all surface as `FileNotFoundError` **winerror 3**; the exception says
nothing useful. So `fix_runner.is_dead` asks a different question: **is the
VOLUME reachable?** Only when the drive/share is present does a missing directory
mean the entry is dead. It also strips surrounding quotes first (cmd.exe does, so
`"C:\Program Files\Foo"` is a working entry), leaves unresolved `%VAR%` alone,
and declines to judge driveless entries (`\foo\bar` resolves against whatever
drive is current). Everything ambiguous is **alive**: a false "alive" costs one
stale entry nobody notices; a false "dead" silently deletes a directory the user
needs. All three of those holes shipped once and were caught in review — each has
a regression test that asks the **real filesystem**, because the original mocked
`isdir` into raising (something it never does) and so passed while the bug was
live.

The predicate lives in `fix_runner`, not `path_prune`, and `path_prune` imports it
back: the runner **re-checks deadness at prune time** and must work as a bare
script with no package context. That re-check is not belt-and-braces — the
engine's cache keys on PATH *text* while deadness is a property of the
*filesystem*, so a tool uninstalled (dead), left unpruned, then reinstalled to the
same location leaves the PATH text — and the hash — untouched, and the stale
verdict would delete a live directory. The queue says what to *consider*; the
filesystem, at prune time, says what to *do*.

**Why a `secret` kind at all.** Elevation is not the only operation that needs a
console — gathering a secret hits the same wall for a different reason, and the
runner is the only place with a TTY to prompt on. Routing a secret through that
console also keeps it out of the hook output and the Claude transcript. It is
designed for, not yet wired up.

### fix-all is user consent for elevation (Windows)

A SessionStart pass must never trigger a UAC or sudo prompt. But when the user
**types `fix-all`**, that is an explicit interactive request for remediation —
so the fix-all re-run of the engine (invoked with the `--fix-all` flag, e.g.
`bash <plugin_root>/hooks/sessionstart/session-bootstrap.sh --console
--fix-all`) handles a non-empty fix queue by **launching the runner itself**:
`Start-Process -Verb RunAs -Wait -PassThru` on the engine's own interpreter, so
the wait covers the real elevated process rather than an unelevated shim that
relaunches itself and exits early. The UAC prompt appears, the engine waits up
to 10 minutes, and on success runs a re-check pass (without `--fix-all` — it can
never loop the prompt) so the elevated items clear in the same cycle. If the
user declines UAC, a task fails, or the wait times out, the engine reports the
outcome and falls back to the run-it-yourself shim.

On Ubuntu/macOS the fix-all run has no TTY for a foreground `sudo` (or a secret
prompt), so the shim remains the only path there. That asymmetry is **encoded,
not remembered**: the engine attaches a `fix_all_cmd` to the aggregate item
exactly when the launch can happen (Windows, and not already inside a fix-all
run — which doubles as the loop guard). Elevation is always an **ASK** outcome
(`_ask_reason` returns `elevation` for the aggregate), so Claude offers it via
`AskUserQuestion`; the `fix_all_cmd` only decides what "Fix" *does* — on Windows
it launches the runner in the same pass, on Unix it points at the shim.

### What the user sees when elevation is the only issue

An elevation-only pass skips the numbered list and the log dump entirely and
emits the aggregate's own focused message:

```
Bootstrap found issues that need admin access: <labels>.

Type 'fix-all' to fix them. You'll be asked to approve an admin prompt.
```

`<labels>` are the queued tasks' labels — an entry's `description` when it has
one, else its `name` (tools read `Install <name>`). On Unix the second line
instead names the shim to run ("It asks for your password where needed"), and a
Windows launch that was declined or failed falls back to the same shim, prefixed
with the launch outcome.

The per-task `needs_elevation` items are **suppressed** from the numbered list
while the aggregate exists, since it speaks for them; without an aggregate they
surface raw rather than vanishing. There is **no `fixed` ritual on this path**:
the env gate re-runs the phase every session until `last_result` is clean, so
confirming would be redundant.

## Display Timing

Bootstrap results (including remediation instructions) surface on the **first user prompt after the engine completes**, not at session start. The SessionStart hook emits suppressed JSON immediately and forks the engine to the background. A UserPromptSubmit hook (`bootstrap-display.sh`) checks for `bootstrap_display.pending` on each prompt; if found, it emits the contents and renames the file to `bootstrap_display.displayed`.

This means the user can start typing immediately. If the engine finishes before the first turn completes, results appear on that turn. If the engine is still running (e.g. slow marketplace fetch), results appear on the next turn after completion.

## User Experience Outcomes

From the user's perspective, there are three possible outcomes on session start:

| What the user sees | What happened |
|--------------------|---------------|
| Nothing | All checks passed (or cache hit) — environment is ready |
| Nothing (first run after install) | Tool was missing, install ran silently, re-check passed — logged internally, no user-visible output |
| Nothing (very first session, fresh machine) | Python was being bootstrapped; the engine runs fully on the next session. No `bootstrap.log` exists yet. |
| Fix-all message | Something needs user action: install failed, no install command, missing config, or external app needs restart |

**Healthy steady state**: The user sees nothing. Bootstrap is working correctly when it's invisible.

**Verifying bootstrap ran**: Check `~/.claude/plugins/data/bootstrap/bootstrap.log`. Entries appear after the first successful engine run. No log file = engine hasn't completed a full session yet (normal on first run of a fresh machine).
