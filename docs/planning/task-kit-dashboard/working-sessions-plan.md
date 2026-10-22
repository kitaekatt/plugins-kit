# task-kit working sessions: implementation plan

Status as of 2026-10-21: units U0-U6 landed (commits under "Implementation units"); U-noindex landed as 38a1e497;
U7 release prep landed as d6bb301a (version 0.13.0, Windows registration refusal, README, register entry); publication
is pending. The user ruled on Q1 and Q2; the rulings are folded into the decisions. Governing
principles for state shared across machines: docs/reference/distributed-truth.md.
Target release:
task-kit 0.13.0 (dev and master both carry task-kit 0.12.0 on 2026-10-21). Publishing task-kit is
pre-approved for this work: `uv run python scripts/publish.py --only task-kit`.

## Goal

Anyone looking at a task -- through the task CLI or the task-kit dashboard, on any of the user's
machines -- can see that a Claude Code harness session is registered as working it, and the machine
that ran the session can tell whether that session's process is still running.

## Binding rulings (user, 2026-10-21, verbatim)

> a claude code session is currently working on 'beta narrative pipeline' and that is not
> reflected in the task-kit interface. This may be an unimplemented feature, not sure. I think I
> should know if a claude code harness is currently working on a task.

> For example, the harness could register its host and process id and state it is working on the
> task within the task file itself. task-kit could validate that process is still running and the
> hostname matches.

> I don't like any dependency on the dashboard running

> if the data isn't in the task file, wherever it goes still needs to be commited, because we run
> this dashboard on multiple machines

Offered "keep task.yaml untouched; each host commits its own dev/tasks/.sessions/<hostname>.yaml
listing which tasks its live Claude sessions work (no merge conflicts: one writer per file, and
that writer is the only host able to verify its own PIDs)", the user answered:

> oh ok sure

> i mentioned including the process so we can verify local machines. For an external machine i
> agree for now that can be unverified. Eventually we will make it so the task-kit service is
> running on all the machines all the time, and then you can ask the other machine if it's valid
> or not, but that's for the future

> the /task hand-off procedure should include unregistering that you are working on a task. The
> /task work procedure should include registering that you are working on a task. If a human
> interrupts this, that is on the human. If you exit a harness I don't think that should count as
> interrupting working on a task.

> oh i suppose if you resume that will change your process id

> we might want to use a different identifier than process id i believe there is a session
> identifier that survives --resume

> i'm probably misremembering

> just use session id if you can't figure it out but there is a better uniquie identifier i am
> prety sure it has 'path' in the name

Readings applied (orchestrator decisions on those rulings): a registration is task-level and lasts
from `/task work` until `/task hand-off`; exiting or crashing the harness does not end it. Records
are keyed on the session id. The "path" identifier is the hook input's `transcript_path`, which is
`<project slug>/<session id>.jsonl` and so derives from the session id; it is recorded as a field,
not used as the key. On a resume that keeps the session id but changes `CLAUDE_PID`, a SessionStart
hook refreshes this machine's record for that session (a write only when such a record exists).

## Premise outcomes

| Premise | Outcome | Evidence |
|---|---|---|
| Claude Code offers a SessionEnd hook usable from a plugin | Confirmed by documentation; no longer used | https://code.claude.com/docs/en/hooks, "SessionEnd": reasons `clear`, `resume`, `logout`, `prompt_input_exit`, `other`; 1.5 s budget that plugin hook timeouts cannot raise. The registration-lifetime ruling (harness exit does not end a registration) removes every use the plan had for it. |
| `task_hold.py` already resolves the harness's Claude pid and create time reusably | Refuted | `handshake.find_session_process` (`task_system/dashboard/handshake.py:40-58`) returns the first ancestor whose name or first three argv words contain "claude". Measured on Claude Code 2.1.306, macOS, from a Bash tool command: it returned the transient `zsh -c "source ~/.claude/shell-snapshots/..."` shell, not the Claude process (process name `2.1.306`, argv `['claude']`, equal to `CLAUDE_PID`). A hold keyed on that shell dies when the command returns and `RuntimeDB.live_holds` deletes it (`runtime.py:542-556`), which matches the empty holds table while the Afterplay session worked beta-narrative-pipeline. Every hold test passes `session_pid` explicitly (`tests/task-kit/test_dashboard_harness_hold.py:142`), so the walk was never exercised. `proctree.ProcIdent` / `live` live in the dashboard package, which `task.py` and the rest of `task_system` may not import (`tests/task-kit/test_dashboard_non_interference.py`, check 1), and their 1.0 s creation-time tolerance is too loose for this use (cross-check finding 4). Documented replacements: `CLAUDE_PID` ("Claude Code sets this to its own process ID in the subprocesses it spawns: Bash and PowerShell tool commands and hook commands ... Requires Claude Code v2.1.214 or later") and `CLAUDE_CODE_SESSION_ID` ("matches the `session_id` field in the hook JSON input and is updated on `/clear`"), both at https://code.claude.com/docs/en/env-vars. |
| `task.py` has git commit/push helpers a registration can reuse; push failure, offline and concurrent pushes can be handled | Partly confirmed | A commit helper exists: `location_ops._git_commit_folder` (pathspec-limited `add` + `commit -- <folder>`, `location_ops.py:333-380`), with `_git_toplevel` (`:307-330`) and `validate.git_ignores_path`. They are private and archive-worded. task-kit has no fetch, pull or push code (`dashboard/vcs.py` is read-only). Push, fetch, integration and the race handling are added work ("Git publication"). |
| Hostname is a stable identifier across the user's machines | Confirmed for this machine; indirect for the fleet; not collision-safe alone | `socket.gethostname()` is `m5pro.local` and `scutil --get HostName` is set explicitly, so it does not follow the network. task_system already uses `resolve.short_hostname()` (`resolve.py:87-89`). bootstrap's env.json `machines` registry is keyed by `gethostname()` and hard-errors on an unknown name (`bootstrap_lib/env_manifest.py:141-160`), so drift on a fleet machine would already surface. Two machines can share a short name, and a WSL instance defaults to its Windows host's name with a separate process table, so ownership adds a machine fingerprint (decision 2). Windows not measured. |
| A `.sessions` directory outside any task folder does not trip validate | Confirmed by code and by measurement | validate's uncommitted check is `git status --porcelain -- <folder>` (`validate.py:123-146`). Measured in a scratch repo: an untracked `dev/tasks/.sessions/m5pro.yaml` left `validate` clean (exit 0) and `list` unchanged; an untracked file inside the task folder made validate warn (exit 1). Discovery globs only `task.yaml` and `*.md` (`discovery.py:274,313`); the inventory takes only directories holding `task.yaml` (`inventory.py:189-197`); a stub cannot begin with `.` (`init.py:65`). |
| HYPOTHESIS: the session id survives a plain `claude --resume` (and `--continue`), and SessionStart with source `resume` carries it | Unmeasured; U0 measures it | The env-vars page says a Bash or hook subprocess "receives the resumed ID" on `--resume <session-id>`. Decision 7 (resume refresh) depends on it: if the id does not survive, the refresh is dropped and a resumed session re-registers through `/task work` (decision 5 takes over the old record). |
| HYPOTHESIS: an `async: true` SessionStart command hook from a plugin runs to completion in an interactive session | Unmeasured; U0 measures it | The hooks page documents `async` for command hooks and says `-p` sessions kill a still-running async hook at teardown. If plugin async SessionStart hooks do not complete, decision 7 writes the refresh synchronously and leaves its publication to the next drain. |
| HYPOTHESIS: a process creation stamp is exact on each platform | Unmeasured; U1 measures it | macOS and Windows record the start time in the kernel and psutil returns it unchanged; Linux psutil derives it from the boot time, which moves with clock steps. Decision 2 picks the stamp per platform; U1 confirms repeated-read stability before fixing tolerances. |

## Cross-check findings (codex sol, verdict "unsound") and dispositions

| # | Finding | Disposition |
|---|---|---|
| 1 | A rejected push stops; a final SessionEnd removal can be lost; a stale record can stay forever | Accepted for what survives. SessionEnd is gone (ruling), so the "last event is the exit" case dissolves. A non-fast-forward rejection is treated as the expected race: re-fetch, integrate, retry, bounded (3 attempts, 60 s). Pending state is derived from git (this machine's session files that differ from upstream) and drained by every later publish from this machine in that repository, by the dashboard on this machine, and by `task sessions publish`. Test: two barrier-synchronized publishers on two clones. |
| 2 | Hostname ownership is not collision-safe | Accepted with one counter-argument, which the reviewer accepted in round 2. Each file carries a machine fingerprint (salted SHA-256 of the OS machine id); a machine owns every file carrying its fingerprint (so a renamed host still owns its old file), refuses to write a file named with its host id that carries another fingerprint, and reads such a file as another machine's. `sessions.host` overrides the host id to resolve a real name clash. Counter-argument to "preferably the filename": the file name stays the host id, because the user accepted `.sessions/<hostname>.yaml`, a reader of the repository should see which machine wrote it, and the future verification service addresses machines by name; the fingerprint inside the file supplies the collision safety. |
| 3 | Moving a session between roots is not crash-consistent | Partly dissolved, rest accepted. The cross-root move came from "one task per session", which the lifetime ruling removes (only hand-off ends a registration). The machine-local session index, which carried the remaining crash-consistency problem, is removed (decision 17); root files are the only state, so no cross-file move remains. |
| 4 | The PID-reuse guarantee contradicts the 1.0 s tolerance it reuses | Accepted. Sessions use a platform-exact stamp (decision 2), not `proctree.live`. The red test uses a stamp 0.5 s off, inside the old window. |
| 5 | Windows ships unmeasured | Mostly dissolved, rest accepted. Detached SessionEnd publication is gone. What remains Windows-specific (`CLAUDE_PID` ancestry from Git Bash and PowerShell, msvcrt locks, the stamp, the async resume hook) is a release gate (U7): measured over ssh on a Windows fleet machine, or the release refuses registration on Windows with a stated reason. |
| 6 | Unit dependencies are wrong (dashboard and hook call CLI verbs another unit owns) | Accepted. CLI, hook and dashboard all call a library API owned by U2/U3 (fixed below); none calls another unit's verb. |
| 7 | Local, ignored and non-git modes conflict with "still needs to be commited" | Accepted, with one counter-argument. A durable root (`dev/tasks`) must be publishable -- a git work tree, the file not ignored, the branch tracking an upstream -- or registration is refused with nothing written. The `local` and `commit` modes are removed; `sessions.record: false` disables recording outright. `tmp` roots stay local-only (machine-local by definition; the dashboard does not show them). Counter-argument: a TRANSIENT failure (offline, retries exhausted, `index.lock` held) keeps the committed record and reports it as unpublished rather than "not registered". Reporting "not registered" while a committed record exists would be false; rolling the record back would make offline work unregistrable and hide the claim from the one machine that can verify it; the pending record publishes at the next drain. Round 2: the reviewer accepted the policy and found the algorithm fetched before committing; fixed by R2-1. |
| 8 | Two guards can pass vacuously | Accepted. The environment guard spawns a child pytest with `CLAUDE_PID` and `CLAUDE_CODE_SESSION_ID` deliberately set and asserts the inner test sees them removed. The hook fast-path test runs the real hook script with a fake interpreter that writes a marker, and asserts no marker when this machine holds no matching record. |
| R2-1 | Offline publication left the record uncommitted (fetch came before commit) | Accepted. Publication commits this machine's session files before any network access, then fetches, integrates and pushes ("Git publication"). The offline test asserts on `git status` and `git log`, not on file presence. |
| R2-2 | `/task work` takeover and Re-check still release a record because its process is not running, which is implicit unregistering on process death; "Re-check" does not say it releases | RESOLVED (Q2): no takeover, Re-check reports and never releases (decisions 5, 11). |
| R2-3 | `/clear` changes the session id, the hook matched only `resume`, and one later `work` would migrate only its own task | Accepted. The SessionStart hook also matches `clear` and re-keys EVERY registration of the calling process by process stamp (decision 7, "Lifecycle"). Test: two registered tasks across clear, exit and resume. |
| R2-4 | Whether another machine's registration blocks this machine's dashboard is consequential and must not be a default | RESOLVED (Q1): another machine's record never blocks (decision 12). |

## Decisions

1. **Storage: `<task root>/.sessions/<host id>.yaml`, one file per machine per physical task
   root.** task.yaml is untouched. With `dev/tasks` linked into the tasks repository this lands at
   `<tasks repo>/<project>/.sessions/<host id>.yaml`, so the record travels with the tasks' own
   version control, the one repository every machine running the dashboard shares. `.plugin-data/`
   in the project root is the wrong home: the project repository can differ from the task root's
   (plugins-kit is public and ignores its `dev/tasks` link).
2. **Identity.** Key: `CLAUDE_CODE_SESSION_ID`. Process: `CLAUDE_PID`, accepted only when it names
   a live ancestor of the caller. Process stamp: on macOS and Windows the psutil creation time,
   compared within 1 ms (float round-trip only); on Linux the procfs start ticks (`<pid>/stat`, field 22)
   plus the procfs kernel boot id (`sys/kernel/random/boot_id`), compared exactly. Host id: `short_hostname()` lowercased,
   `-wsl` appended under WSL unless the name already ends in `wsl`, or `sessions.host` when set.
   Machine fingerprint: the first 16 hex digits of SHA-256 over `task-kit-machine-v1:` plus the OS
   machine id (macOS `IOPlatformUUID`, Linux and WSL systemd `machine-id`, Windows `MachineGuid`);
   hashed so no hardware identifier is committed; an unreadable machine id refuses registration.
   `transcript_path` is recorded home-relative (decision 15).
3. **Lifetime (ruling).** A registration starts at `/task work` and ends at `/task hand-off`.
   Harness exit, crash and resume do not end it. Nothing removes a record because its process died.
4. **`task.py work` registers, after its validation gate passes** -- mechanically, because a skill
   step depends on the agent remembering it, and the handshake's broken PID went unnoticed because
   nothing mechanical exercised it. **The hand-off procedure releases** with a skill step,
   `task.py sessions release <ref>`, after self-verify and before the baton: hand-off has no CLI
   verb, and release must be the last state change, after `update` has been re-run until its
   findings are clean.
5. **Takeover at `/task work` on the same machine.** For the same task, a record whose session id
   equals the caller's, or whose process stamp equals the caller's process (a `/clear` whose hook
   did not run), is refreshed in place. A record by another session is kept whether its process is
   running or not: the new registration coexists and the old one is reported
   (`sessions.not_running_takeover_policy` refuses takeover). Another machine's record cannot be
   edited by this machine (one writer per file); `work` reports it. Resolution of Q2.
6. **A session may hold several registrations.** Only hand-off ends one, so `/task work` on a second
   task leaves the first registered and prints `session: this session also holds <id>`.
7. **Session-start refresh: matchers `resume` and `clear`** (one SessionStart hook, `async: true`;
   it writes nothing when this machine holds no matching record).
   - `resume` (hypothesis until U0): the session id is kept and `CLAUDE_PID` changes, so every
     record of this machine carrying that session id gets the resumed process's stamp and
     `transcript_path`, then publishes. If U0 shows the id does not survive a plain resume, the
     `resume` half is dropped and the resumed session's `/task work` takes the record over
     (decision 5).
   - `clear`: the session id changes and the process does not, so every record of this machine
     whose process stamp equals the calling process is re-keyed to the post-clear session id, in one
     locked operation ("Lifecycle", re-key), then publishes. Every
     registration of the session moves, not only the next one `/task work` touches.
8. **Publication runs inside the command that changed the record** (work, release, dismiss, reconcile, the
   session-start hook), bounded, with retry on the push race; pending files publish with the
   next publish from this machine in that repository, from the dashboard, or from
   `task sessions publish`.
9. **Git never publishes work that is not this machine's session records, and never rewrites.**
   Integrate upstream (fast-forward, else merge) only when every local-only commit touches only this
   machine's session files; push only when the net diff from upstream names only those files. No
   force, rebase, reset, stash or branch switch.
10. **Durable roots must be publishable; `tmp` roots are local-only; `sessions.record: false`
    turns recording off** (finding 7).
11. **What ends a record**: release at hand-off; `task sessions release <ref> --all-local`, the
    human's explicit cleanup after an interruption ("that is on the human"); a dismissal
    (decision 16); a machine's own records on tasks that are closed, archived or absent, dropped at
    that machine's next write in that root (no workable task remains to work). Re-check reports and
    never releases; the dashboard has no Release button (release is `task sessions release`).
    Nothing ends a record because its process died. Resolution of Q2.
12. **Holds: ONLY this machine's verified-running record holds a task** in its dashboard. This
    machine's not-running records and every other machine's records are display-only and never
    block. User (Q1, verbatim): "We never prevent an action because of an unverifiable state".
13. **Read verbs never write.** `list`, `show`, `status` and `task sessions` (list) stay read-only
    and offline.
14. **No heartbeat commits**; every display shows the record's age. **Item-level "in flight" is
    unchanged**: the `inflight-evidence` item (deferred by the user in
    dev/tasks/task-kit-extraction) keeps its UX review; this feature supplies the evidence it needs.
    **Codex and other harnesses are out of scope** (`harness: claude-code` leaves room).
15. **`transcript_path`**: from the hook input when a hook writes; from `work`, the single file
    matching `${CLAUDE_CONFIG_DIR:-~/.claude}/projects/*/<session id>.jsonl`. Stored home-relative
    (`~/...`); omitted when not under the home directory, or when zero or several files match (a
    `session:` note says which). The project slug inside it encodes the project directory as Claude
    Code names it; recording it follows the user's request to locate the transcript, and
    `sessions.record: false` remains the opt-out.
16. **A dismissal is authoritative.** The owner drops the dismissed record at its next write even
    if its process looks alive; the dismisser drops its tombstone once the target is gone (kept
    while any session file in the root is unreadable). The dashboard's Dismiss, in task details,
    targets unverified markers, which are tinted with "unverified" in title and aria-label. User
    (verbatim): "The human dismissing is contribution of information" and "6 is just a valid
    information signal as the user has visibility across machines." and "We should be able to
    dismiss fields that  are unverified. Unverified can be shown through a tint or other visual
    differentiation".
17. **No machine-local session index and no publish log.** The git-tracked `.sessions` files are
    the only registration state; only information-free OS locks stay local. User (verbatim): "I am
    not convinced we need anything besides global state tracked into git." Principles:
    docs/reference/distributed-truth.md.

## File format

Written atomically (temporary file in the same directory, then `os.replace`), keys in the order
shown, records sorted by `(task, session_id)`; an unchanged set is never rewritten:

```yaml
_schema_version: 1
host: m5pro                    # equals the file name
machine: 3f9c0a1b7d2e4c55      # machine fingerprint (decision 2)
os_instance: macos             # macos | linux | linux-wsl | windows
sessions:
  - session_id: e4a0712e-91f0-4c7f-bdd5-61c7ea094a4a
    harness: claude-code
    task: beta-narrative-pipeline           # stub under this task root
    checkout: Afterplay2                    # basename of the project root it worked from
    transcript_path: ~/.claude/projects/<project slug>/e4a0712e-91f0-4c7f-bdd5-61c7ea094a4a.jsonl
    registered_at: "2026-10-21T18:04:11Z"
    process:
      pid: 46063
      created: 1792613951.174184            # macOS and Windows stamp
      # Linux adds boot_id and start_ticks, which are the compared stamp there
      recorded_at: "2026-10-21T18:04:11Z"   # registration or resume refresh
dismissed:                      # optional; omitted when empty
  - host: 5090w                 # the dismissed record's file
    session_id: 7b1c0d52-0000-4000-8000-000000000000
    registered_at: "2026-10-20T09:00:00Z"
    dismissed_at: "2026-10-21T18:10:00Z"
```

- No absolute paths. `checkout` is display only; records match tasks by physical task root plus
  stub, so plugins-kit, plugins-kit2 and plugins-kit3 (one physical `dev/tasks`) agree.
- `dismissed:` lists tombstones, written only by the dismisser into its own file; records sort by
  `(host, session_id)`. Semantics: decision 16.
- An empty set is `sessions: []`; the file is never deleted.
- A file that does not parse or validate is reported by every reader as a `note:` naming the file
  and the reason, and its records are ignored. Its owner refuses to overwrite it.

No machine-local session index and no publish log (decision 17). Only OS locks stay local, in the
user-scoped data directory (`${CLAUDE_BOOTSTRAP_DATA_ROOT:-~/.claude/plugins/data}/plugins-kit/task-kit/sessions/locks/`),
taken per physical root and per repository, never nested out of that order.

## Record states

| State | Condition | Shown as |
|---|---|---|
| `running` | file owned by this machine (fingerprint), the process stamp matches a running process | `m5pro running (Afterplay2, 2h)` |
| `not running` | file owned by this machine, no process matches the stamp; kept (decision 3) | `m5pro not running (registered 2d ago)` |
| `unverified` | another machine's file | `5090w unverified (registered 3h ago)`; tinted marker, "unverified" in title and aria-label, dismissable |
| reserved: `confirmed running`, `confirmed not running` | answered by the owning machine's task-kit service | not built ("Future direction") |

Only `running` holds a task (decision 12). A record on a closed, archived or absent task is shown with that fact (`... on a closed task`)
until its owner's next write drops it (decision 11).

## Lifecycle

**Register** (`task.py work <ref>`, after the gate). Nothing happens when `CLAUDE_CODE_SESSION_ID`
is unset (not a Claude Code subprocess). Inside a dashboard run (`TASK_KIT_AGENT_DEPTH` set):
`session: not registered` (the dashboard shows its runs, and a run must not commit). With
`sessions.record: false`: `session: not registered: sessions.record is false`. Otherwise: resolve
identity; preflight the root (durable roots: git work tree, file not ignored, upstream present --
else `session: not registered: <reason>` and nothing written); take the root lock; apply decision 5 and drop this machine's
records on closed, archived or absent tasks and dismissed records; add or refresh the record; write; release the locks; publish.

**Release** (`task.py sessions release <ref>`, the hand-off step). Removes this machine's records
for the task whose session id or process stamp is the caller's; `--all-local` removes every record
this machine holds for the task. Publish after the write.

**Resume refresh** (decision 7). The hook finds this machine's records for
`CLAUDE_CODE_SESSION_ID` in the task roots' session files, updates their stamp and `transcript_path`,
and publishes; with no matching record it writes nothing. A forked session (`--fork-session`, source `fork`) gets a fresh
id and registers through `/task work`.

**Clear re-key** (decision 7). With the calling process's stamp S and the post-clear id N, the hook
rewrites, under each root lock, every record of this machine whose stamp is S to session id N, then
publishes; with no such record it writes nothing. Each step is idempotent, so a re-run after an
interruption completes the move, and the root files are the truth throughout. Resuming the
conversation from before the `/clear` afterwards finds no records under its id (they moved to N)
and registers again through `/task work`.

**Dismiss and reconcile.** `sessions.dismiss` writes a tombstone into the dismisser's own file
(decision 16). `sessions.reconcile` runs on the owner: it drops dismissed records and closed,
archived or absent ones, and drops the dismisser's tombstones whose target is gone, then publishes
its own file.

**Interruption.** A session that ends without hand-off leaves its registration in place, shown as
`not running` on its own machine and `unverified` elsewhere, until a human releases or dismisses it (per the
ruling, that is on the human).

## Git publication

`session_publish.publish(repo)` holds the per-repository lock, runs every git command with
`GIT_TERMINAL_PROMPT=0`, stdin closed, 20 s bounds on network commands and 30 s on local ones, and
makes at most 3 network attempts within 60 s.

First, before any network access (R2-1): commit each of this machine's session files in the
repository that differs from HEAD -- `git add -- <file>` only when untracked, then
`git commit -q -m "task-kit sessions: <host id>" -- <files>` (pathspec commit; other staged content
is left alone). A commit failure stops here (`not committed`). From this point the record is
committed whatever the network does. Then each network attempt:

1. `git fetch` the upstream. A failure stops (`not published: fetch failed`).
2. If any local-only commit (merge-base..HEAD) touches a path that is not this machine's session
   file: stop, `not published: unpushed commits outside this machine's session records`.
3. If HEAD is an ancestor of upstream: `git merge --ff-only`. Else, if upstream has commits HEAD
   lacks: `git merge --no-edit -q <upstream>`. Because the record commit comes first, a branch that
   was behind is integrated by a merge commit rather than a fast-forward; that is the cost of never
   leaving a record uncommitted. Content conflicts cannot occur, because this machine's commits
   change only its own files and no other writer changes those; a merge git refuses to start
   (uncommitted edits on incoming paths) leaves no merge state and is reported; a merge that stops
   with conflicts anyway is aborted (`git merge --abort`) and reported.
4. If the net diff `git diff --name-only <upstream> HEAD` is non-empty and names only this
   machine's session files: `git push -q <remote> HEAD:<upstream branch>`. A non-fast-forward
   rejection starts the next attempt; any other failure stops.

| Situation | Outcome |
|---|---|
| durable root not a git work tree, file ignored, no upstream | registration refused before any write |
| `tmp` root | `local only: tmp is machine-local` (no git) |
| in step | `published` |
| behind upstream | merge (record commit first), push -> `published` |
| another machine pushed first | retried: fetch, merge, push -> `published` |
| retries exhausted | `registered on this machine; committed; not published after 3 attempts: <last git line>` |
| offline, auth failure, fetch timeout | `registered on this machine; committed; not published: fetch failed: <line>` |
| fast-forward or merge refused (local edits on incoming paths) | `registered on this machine; not published: <line>` |
| unpushed commits by others | `registered on this machine; not published: unpushed commits outside this machine's session records` |
| commit fails (`index.lock` held, a commit hook) | `registered on this machine; not committed: <line>` |

Every "not published" line ends with "other machines cannot see it until this machine's next
publish (`task sessions publish`)". The file is a whole-state snapshot, so any later publish carries
everything earlier ones missed. Pending files are derived, not recorded: this machine's session
files that are uncommitted or differ from upstream after the last fetch.

## Contract changes

The prior ruling that the DASHBOARD changes no verb contract still holds: every verb change below
rests on the user's 2026-10-21 rulings, and the dashboard calls the library, not a verb.

**`work`**: exit codes, the validation gate and stdout (the initialization block) are
byte-identical. Added after a passing gate, inside a Claude Code subprocess only: the registration
(a write under `<task root>/.sessions/`, a publish) and stderr lines prefixed
`session:`:

- `session: registered <host id> on <task id> (session <first 8>); <publication outcome>`
- `session: also working this task: <host id> <state> <age> (session <first 8>)`, per other record
- `session: this session also holds <task id>[, ...]`
- `session: not registered: <reason>` -- exit stays 0, because the gate passed and work may proceed

**Hand-off (skill)**: a step between self-verify and the baton runs `task.py sessions release
<ref>` and relays its `session:` line in one sentence.
**`list`**: a task line with records gains a trailing ` [working: <host id> <state>[ <age>]; ...]`
after the title; the first four two-space fields are unchanged. `--format json|yaml` gain
`sessions` per task.
**`show`**: one `working: <host id> <state> (<checkout>, session <first 8>, since <time>)` line per
record, or `working: -`.
**`status`**: a `sessions:` block in the substrate.
**`sessions`** (the fifteenth verb): default lists records with states and this machine's pending
files (`--scope project|all`, `--format text|json`); `release <ref> [--all-local]`; `publish`.
Release is the only CLI way to end a record.
**Skill**: the work capability relays any `session:` line in one sentence; a `sessions` capability
routes "who is working on X".
**Handshake**: `task_hold.py` takes its PID and session id from decision 2; otherwise unchanged.
`find_session_process` stays as a shim emitting `DeprecationWarning` for two minor versions
(docs/reference/deprecation-policy.md).
**Plugin hooks**: `plugins/task-kit/hooks/hooks.json` gains one SessionStart hook with matchers
`resume` and `clear` (decision 7). task-kit ships no other hook.
**Dashboard**: each task row carries `sessions` (host id, state, age, checkout, short session id)
and a badge; the details list them. `held` is "runtime hold OR this machine's verified-running
record" (decision 12), with the reason naming the machine and how to release. Re-check reports and
never releases; there is no Release button. Unverified markers are tinted ("unverified" in title and
aria-label) and the details carry Dismiss (`POST /api/v1/sessions/dismiss`, decision 16). A
background `SessionsMaintainer` runs `sessions.reconcile` and publishes this machine's own file every
60 s. The snapshot revision covers records and states.
references/dashboard.md changes accordingly, including the blocked-run resolution flow
(`/task work`, fix, `/task hand-off`, then Re-check).

## Cross-cutting cases

- **One folder, several checkouts.** Records key on physical root plus stub; the inventory reads
  each physical root's `.sessions` once, as it assigns each physical task folder once.
- **Several sessions on one machine.** One file, many records; the root lock serializes
  read-modify-write; the repository lock serializes git work.
- **Windows.** `CLAUDE_PID` is documented for Bash and PowerShell tool commands; locks use `msvcrt`;
  host ids are lowercase for case-insensitive filesystems; the hook runs through `bash` as the
  shipped hooks do. Release gate in U7.
- **Tests run inside Claude sessions.** pytest inherits `CLAUDE_PID` and `CLAUDE_CODE_SESSION_ID`,
  so a test running `task.py work` would register real sessions and push. `tests/task-kit/conftest.py`
  removes both at import and redirects the data directory (guard: finding 8).
- **Dashboard runs.** Never register.

## Razor, write paths, directives (submit gates)

| Opinion | Verdict |
|---|---|
| Automatic commit, fetch, integrate and push of session records | Passes, so configurable: `sessions.record` (default `true`). Scenarios: tasks tracked in a project repository whose pushes start CI or whose branch is protected; a repository readable by people who should not see host names or transcript paths. `false` records nothing (finding 7: no undistributed state). |
| A registration survives harness exit until hand-off | Passes for a user who ends sessions without hand-off (registrations accumulate and stay listed); declined on the user's 2026-10-21 ruling. U7 adds a register entry in plugins/CLAUDE.md; the remedy is `/task hand-off`, `task sessions release --all-local`, or `sessions.record: false`. |
| Only this machine's verified-running record holds its dashboard | Follows from decision 12: no action is prevented by an unverifiable state. |
| Host id from the hostname | Not an opinion (an observed fact); `sessions.host` exists only to resolve a detected collision, which refuses registration with a message naming the key. |
| No heartbeat | Fails: the age is always shown. |

Write paths: `<task root>/.sessions/<host id>.yaml` is project-durable (task-kit's own durable task
root; reason in decision 1). The locks are user-scoped. None derives from a
script's location. Directives: the added skill lines (relay `session:` lines; run release before
the baton) are true, claim no authority, and keep the user informed. No worktree text. No
cross-plugin import.

## Implementation units

Paths are relative to `plugins/task-kit/skills/task/scripts/` unless they start with `plugins/`,
`tests/` or `docs/`. Each unit runs its targeted tests (`uv run --extra dev pytest
tests/task-kit/<file> -v`) and shows each listed check failing first.

Library API (fixed so U4, U5 and U6 depend on U2/U3 only):
`sessions.register(identity, machine, project_root, ref)`, `sessions.release(identity, machine,
project_root, ref, all_local=False)`, `sessions.refresh(identity, machine, transcript_path)`,
`sessions.rekey_after_clear(identity, machine)`, `sessions.read_root(task_root, machine)` (records
with states and notes), `sessions.dismiss(...)`, `sessions.reconcile(...)`;
`session_publish.preflight(task_root)`, `session_publish.publish(repo_root, machine)`,
`session_publish.pending(repo_root, machine)`. Dashboard route `POST /api/v1/sessions/dismiss`;
background `SessionsMaintainer` (60 s reconcile and publish of its own file).

| Unit | Scope | Owns | Depends on |
|---|---|---|---|
| U0 probe (e6b51a76) | With a scratch plugin carrying only SessionStart hooks that log their input and environment: start a real session; record `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_BRIDGE_SESSION_ID` (if set), `CLAUDE_PID` and `transcript_path`; exit; repeat after `claude --resume <id>`, `claude --continue` and `claude --resume <id> --fork-session`; compare; then `/clear` inside a session and confirm the SessionStart source `clear` carries a different id while `CLAUDE_PID` is unchanged. Record the SessionStart `source` and `session_id` in each case, whether an `async: true` plugin SessionStart hook completes in an interactive session, and `CLAUDE_PID` ancestry from Git Bash and PowerShell on a Windows fleet machine (ssh). Settles the two session hypotheses; if the session id does not survive a plain resume, decision 7 is dropped. | `docs/reference/claude-code-plugin-platform.md` (a "Session identity across resume, measured" section) | none |
| U1 identity (579b8d24) | `task_system/harness.py` (identity, ancestry, dashboard-run detection, transcript lookup); `task_system/machine.py` (host id, fingerprint, `os_instance`); `task_system/procstamp.py` (platform stamp; measures repeated-read stability per platform); `task_system/oslock.py` (OS file lock; `mutation.ProcessLock` delegates); `task_system/datadir.py` (resolver moved from `dashboard/state.py`, re-exported); handshake on the identity, with the shim | those five modules; `task_system/dashboard/{mutation,state,handshake}.py`; `task_hold.py`; `tests/task-kit/{test_task_harness_identity.py,fake_claude.py,conftest.py,test_dashboard_harness_hold.py,test_task_env_sanitized.py}` | none |
| U2 store (3bcba119) | format, validation, fingerprint ownership and collision refusal, per-session and per-root locks, states, register / release / refresh / clear re-key / dismiss / reconcile / drop-closed, reading every file of a root | `task_system/sessions.py`; `tests/task-kit/test_task_sessions_store.py` | U1 |
| U3 publisher (5150783d) | preflight, the attempt loop and its rules, pending detection, `publish.log` | `task_system/session_publish.py`; `tests/task-kit/{test_task_sessions_publish.py,git_remote_support.py}` | U1 |
| U4 CLI and skill (e45181cf) | `work` registration and `session:` lines (in `_cmd_work` after `state_ops.work` returns; `state_ops` untouched); `sessions` verb; `list`/`show`/`status`; docstring; SKILL.md (work relay, hand-off release step, `sessions` capability); the `sessions:` keys of `task.local.yaml` | `task.py`; `task_system/listing.py`; `plugins/task-kit/skills/task/SKILL.md`; `tests/task-kit/test_task_sessions_cli.py` and any task-kit test asserting list/show/status/work text | U2, U3 |
| U5 session-start hook (d95665c5) | `hooks.json` (SessionStart, matchers `resume` and `clear`, `async: true`); the bash 3.2/zsh-safe script (forced interpreter chain as in `plugins/presence-kit/hooks/stop/presence-stop.sh`; `task.py` re-execs into the venv), dispatching on the input's `source` | `plugins/task-kit/hooks/hooks.json`; `plugins/task-kit/hooks/sessionstart/session-start.sh`; `task_session_start.py`; `tests/task-kit/test_task_session_start_hook.py`; the allowlist entry in `tests/repo-scripts/test_python_invocation_standard.py` | U2, U3, U0 |
| U6 dashboard (283b4ec5) | inventory read of `.sessions` per physical root; task-row `sessions`; revision; `held` from registrations; Re-check reports only; dismiss route; `SessionsMaintainer`; badge and details | `task_system/dashboard/{inventory,snapshot,scheduler,server}.py`; `task_system/dashboard/static/{app.js,app.css}`; `plugins/task-kit/skills/task/references/dashboard.md`; `tests/task-kit/{test_dashboard_sessions.py,test_dashboard_inventory.py,test_dashboard_ui_contract.py,ui_harness.js}` | U2, U3 |
| U-noindex (landed as 38a1e497) | Remove the machine-local session index and `publish.log`; records are found from the root files only (decisions 16, 17) | `task_system/sessions.py`, `task_system/session_publish.py`, the hook script and their tests | U2, U3, U5 |
| U7 docs, Windows gate, release | register entry (plugins/CLAUDE.md: registration lasts until hand-off; the git-privilege entry extended to session publication); README; design doc section; version 0.13.0 in plugin.json and pyproject.toml; Windows smoke over ssh (or ship with Windows registration refused and say why); review; claudx smoke; publish | `plugins/CLAUDE.md`; `plugins/task-kit/{README.md,.claude-plugin/plugin.json,pyproject.toml}`; `plugins/task-kit/skills/task/design/task-system-design.md` | all |

Order: U0 and U1 in parallel; then U2 and U3 in parallel; then U4, U5 and U6 in parallel; then U-noindex; U7 last.

## Tests (each shown to fail by the stated revert)

1. Identity: a fake-Claude parent sets `CLAUDE_PID` to itself and runs a child through `bash -c`
   with `.claude/shell-snapshots` in its argv. The resolver returns the fake Claude; reverting the
   handshake to `find_session_process` returns the shell (red).
2. Identity refusals: `CLAUDE_PID` naming a non-ancestor; `CLAUDE_PID` unset with
   `CLAUDE_CODE_SESSION_ID` set; an unreadable machine id.
3. Machine: a same-name file with another fingerprint is refused for writing and read as foreign
   (revert the fingerprint check -> the foreign file is overwritten, red); a renamed host still owns
   its old file; the WSL rule with a faked kernel release; `sessions.host` override.
4. Stamp: a stamp 0.5 s off reads `not running` (revert to the 1.0 s tolerance -> `running`, red);
   a live sleeper reads `running`; a killed sleeper reads `not running` and its record is KEPT
   (revert: prune on death -> the record disappears, red).
5. Store: same-session refresh; same-process refresh when the clear hook did not run; a running
   other session is kept; a not-running other session is kept beside the new registration and
   reported; release by session id and by process stamp;
   `--all-local`; records on closed, archived or absent tasks dropped at the owner's next write;
   a dismissed record is dropped by its owner at the next write although its process is alive,
   and the tombstone is dropped once the target is gone but kept while a session file is unreadable;
   an unchanged set rewrites nothing; an invalid own file is not overwritten.
6. Publisher, against a bare remote and clones: offline first -- with the remote unreachable,
   `git status --porcelain -- <file>` is clean and `git log -1 -- <file>` shows the record commit
   (revert to fetch-before-commit -> the file is still modified, red); in step; behind then merged
   and pushed; two
   barrier-synchronized publishers on two clones both reach the remote (remove the retry -> one is
   missing, red); a racing upstream task-record commit is merged and the push succeeds; a local
   foreign commit blocks integration and push (remove the guard -> the foreign commit reaches the
   remote, red); exhausted retries name the last git line; ignored, non-git and no-upstream durable
   roots refuse registration with no write and no commit; `tmp` stays local; pre-staged unrelated
   content survives; the committed pending file from the offline case is delivered by a later
   publish.
7. `work`: stdout byte-identical with and without registration; each `session:` line; nothing
   without `CLAUDE_CODE_SESSION_ID`; refusal under `TASK_KIT_AGENT_DEPTH` and with
   `sessions.record: false`; exit 0 on a registration failure.
8. Read verbs never write: `list`, `show`, `status` and `sessions` leave the tree and the data
   directory byte-identical with `not running` and foreign records present.
9. Session-start hook: the real script with a fake interpreter that writes a marker -- no marker
   when this machine holds no matching record (revert the fast path ->
   marker written, red); with a matching record the stamp and `transcript_path` refresh; no write
   when the session holds no record; `hooks.json` shape; bash 3.2 constructs only. Scenario: one
   session registers two tasks, then `/clear` -> BOTH records move to the post-clear id
   (revert to re-keying only the record `/task work` touches -> one record keeps the old id, red);
   then exit -> both stay, `not running`; then resume of the post-clear id -> both refresh to the resumed
   process; then hand-off of one task releases only that one. The re-key is also interrupted after
   each phase and re-run to completion.
10. validate, list and the inventory ignore a dirty `.sessions` sibling (control: the same file
    inside the task folder trips validate).
11. Dashboard: one read per physical root across three checkouts; a verified-running
    local record holds the task (revert -> the scheduler starts a run, red); a not-running local
    record and a foreign record never hold; Re-check releases nothing; the dismiss route; the revision changes when a process dies;
    the background publish touches only this machine's files.
12. Environment guard: a child pytest started with `CLAUDE_PID` and `CLAUDE_CODE_SESSION_ID` set
    sees both removed (remove the conftest removal -> the child sees them, red).

## Release (U7)

Run the targeted files, then the task-kit directory serially; `git-kit:git-code-review` over the
range; Windows gate; `claudx --all` smoke in a scratch project whose `dev/tasks` is a clone of a
scratch bare remote (`/task work` on two tasks, confirm the records and their push; `/clear`,
confirm the re-key; exit and resume, confirm the refresh; `/task hand-off` of one, confirm the
release and its push); then `uv run python
scripts/publish.py --only task-kit`, and confirm that a real `/task work` in Afterplay produces a
pushed record in the tasks repository. `requires_bootstrap` stays 0.138.0.

## Future direction (not built)

When a task-kit service runs on every machine, a reader can ask the owning machine to verify a
record by `(session_id, process stamp)` and show `confirmed running` or `confirmed not running`
instead of `unverified`; the owning service could also release on request, which would let a
dashboard on one machine clear another machine's stale registration. The record carries every
field such a query needs, the host id is the natural address key (env.json's `machines` registry
holds each machine's tailscale name), and the two state names are reserved above.

## Questions (resolved)

- **Q1 (R2-4). RESOLVED.** Another machine's registration never blocks this machine's dashboard.
  Only this machine's verified-running record holds a task. User (verbatim): "We never prevent an
  action because of an unverifiable state". Unverified markers are dismissable (decision 16).
- **Q2 (R2-2). RESOLVED.** A not-running record is neither taken over by `/task work` nor released
  by Re-check; the new registration coexists, the old one is reported, and only an explicit release
  (`task sessions release <ref> --all-local`) or a dismissal ends it (decisions 5, 11, 16).
