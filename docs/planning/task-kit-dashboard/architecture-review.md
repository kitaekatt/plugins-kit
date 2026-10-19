# Review: dashboard-architecture.md

Unit: architecture-review (cross-check of the plan checkpoint). Reviewer: Claude
(read-only; this file is the only write). Date: 2026-10-17.

Source paths below are relative to D:\dev\plugins-kit3 unless they start with
`design/`, `research/` or `plan.md`, which are relative to this task folder.

## Verdict

REVISE.

The overall shape is sound. Task folders stay the source of truth, there is one
flat job per item, one ledger per run, polling, loopback plus token, and port 0
plus PID identity. Four defects block approval. Two conflict with a binding
ruling or with shared-tree discipline. The other two let an unattended worker
break the publication and force-push floor, or reject legitimate runs in the
repository the dashboard is built for. The "CONFIRMED" flat-job premise is also
wrong in both directions. One of the two llm-kit format changes it requires is
unnecessary. And it misses a placement trap that would turn the worker into a
text-only completion.

## Findings

### 1. blocking -- Project-wide harness exclusion interrupts in-harness task use

Section: "Non-interference with harness task use" (design lines 309-316), and
decision 5.

Evidence:
- The design makes "a live runner and an interactive hold ... mutually
  exclusive in one project". The hold is keyed to the Claude session's ancestor
  PID and lasts until "that process exits".
- So while a dashboard job runs (default 7200 s) in plugins-kit, `/task work` on
  ANY task in plugins-kit is refused in the harness. Conversely, one in-harness
  `/task work` locks out dashboard runs for the whole project until Claude Code
  exits.
- This conflicts with the binding ruling: "continue to use the task system in
  the claude code harness uninterrupted ... free and automatic as the dashboard
  runs in parallel to the task system versus changing its foundation".
- It also exceeds what the repository requires. Parallel writers in one tree are
  the documented norm (root CLAUDE.md, "Worktrees and scratch copies", rule 1).
  Recorded default (4) limits AUTONOMOUS tasks per project. It does not limit
  in-harness work.

Change:
- Scope the exclusion to the task folder, not the project.
- Make it one-directional: the dashboard yields, the harness never refuses.
  `/task work <ref>` registers a hold on that task only. The scheduler will not
  START a run on a held task. If a run is already live on that task, `/task
  work` prints one notice line (run id, how to cancel it) and proceeds unchanged.
- Release the hold when the session ends or on Re-check, as designed.
- With no dashboard online, the handshake stays a no-op.
- Add a test that `/task work` on task B succeeds while a run is live on task A
  in the same project.

### 2. blocking -- The Codex worker has no enforced floor against push, publish, or tree destruction

Sections: "Codex worker" (lines 242-248), "Shared write policy" (lines 249-256).

Evidence:
- The shared policy says workers "may not ... commit, push, publish, rewrite
  history". Only Claude gets an enforcement mechanism (`--disallowedTools`).
- For Codex the design emits `-s workspace-write` with network on by default
  (line 247). bootstrap_lib/codex.py:323-324 then adds
  `sandbox_workspace_write.network_access=true`.
- With network and the user's git credentials, an unattended Codex worker can
  run `git push --force` or `scripts/publish.py`, the repository's one
  user-gated action (root CLAUDE.md, "Publishing", "Gotcha 4"). The design's
  only backstop, "record initial HEAD and branch and block on any change", never
  sees a push, because a push leaves local HEAD and branch unchanged.
- CodexCliBackend drops `disallowed_tools` (adapters/codex_backend.py:300-303),
  so the Claude floor does not carry over.

Change:
- Default Codex network to false for task workers, and keep it as a
  configurable opt-in that the UI discloses per run.
- Alternatively, add a live characterization test proving a Codex-side command
  deny for `git push`, `git commit`, `git reset`, `git stash`, `git clean`,
  `git checkout`, `git restore`, `git switch`, `git branch`, `git worktree` and
  `publish.py`. Refuse to launch Codex when that test is absent or fails, which
  matches the Claude rule at line 240.
- Either way, state the floor per worker in the doc. Today the shared policy
  claims something the mechanism does not enforce, which violates the
  checkable-instructions rule (root CLAUDE.md, "Instructions we ship to Claude
  must be checkable").

### 3. blocking -- The "HEAD unchanged" check rejects legitimate runs in a shared tree and cannot attribute commits

Sections: "Prompt and result contract" (line 209-211: acceptance "requires ...
unchanged HEAD/branch"), "Shared write policy" (lines 253-254).

Evidence:
- In plugins-kit, other sessions commit and push to `dev` freely and
  continuously (root CLAUDE.md, "Committing and pushing to `dev` is unrestricted
  ..."). The user does too, in the harness, while runs are live.
- A two-hour run will regularly see HEAD move for reasons unrelated to the
  worker, so the run blocks as a breach.
- Ancestry cannot tell a worker commit from another session's commit. The
  check is therefore both noisy and unable to prove what it claims.
- The floor in recorded default (6) and in lines 237-239 does not cover the
  in-checkout commands that destroy OTHER sessions' uncommitted work: `git
  stash`, `git checkout -- <path>`, `git restore`, `git clean`, and `git add`
  sweeping someone else's WIP. Root CLAUDE.md treats the index and working tree
  as shared state ("Anti-pattern: unstaging another session's work").

Change:
- Prevent instead of detect. For Claude, the job-level `--disallowedTools`
  denies `git commit`, `git push`, `git reset`, `git rebase`, `git stash`,
  `git clean`, `git restore`, `git checkout`, `git switch`, `git branch`,
  `git worktree` and `publish.py`. For Codex, see finding 2.
- Relax acceptance to two checks: the branch name is unchanged, and start HEAD
  is an ancestor of end HEAD (no rewrite). Record any new commits in the
  receipt as observed, not attributed.
- Record `git status --porcelain` at start and end and show the delta in run
  history as "paths changed during run (any writer)". Never gate on it.

### 4. blocking -- Premise correction: the deny floor's placement in llm-kit silently decides whether the worker has tools

Sections: "Claude worker" (lines 234-241), "Premise outcomes" (first bullet,
lines 431-434).

Evidence:
- A run-level floor (`JobFile.disallowed_tools`, model.py:847, or `jobs run
  --disallowed-tools`) passes through `_require_floor_subjects` (jobs/run.py:679-720,
  1026-1031).
- That function maps any `Bash(...)` token to the SHELL_EXEC subject by its head
  (completion/capabilities.py:109-127). It then requires that guarantee, and
  `arm_call` switches the backend to TEXT-ONLY mode (completion/requirements.py:382-383).
- For Claude, text-only replaces `--permission-mode bypassPermissions
  --allowedTools`, and a non-empty `allowed_tools` is refused with "nothing was
  dispatched" (adapters/backends.py:694-699, 714-722).
- For Codex, text-only forces `-s read-only` (adapters/codex_backend.py:712-717).
- So the natural spelling of the floor makes every job either refuse or run as
  a tool-less completion. Only `Job.options.disallowed_tools`, which is merged
  without subject arming (run.py:774-806), gives the intended agentic worker
  with a deny list.
- The CONFIRMED premise does not mention this.

Change:
- Specify that the floor goes in `Job.options.disallowed_tools`, never in
  `JobFile.disallowed_tools` or the run flag.
- Add a test asserting that the emitted Claude argv contains both
  `bypassPermissions` and every floor pattern, and that Codex argv contains
  `workspace-write`.
- Downgrade the premise to "CONFIRMED with constraints" and list this
  constraint.

### 5. should -- Cut the per-job `timeout_s` llm-kit format change, and reserve an acceptance budget

Sections: "Emitted one-job file" (line 195), the llm-kit capabilities list
(lines 286-293), Increment 3.

Evidence:
- Each run holds exactly one job (line 191). The run-wide `jobs run --timeout`
  (cli/jobs.py:74, default 900 at run.py:69) is therefore already that job's
  timeout.
- No resume path exists in v1 (lines 230-232), so nothing re-reads a persisted
  value.
- The proposed change revises llm-kit's frozen job definition for no v1 gain.
- Separately, one budget covers model and contract: the contract gets
  `timeout_s - completion_elapsed_s` (run.py:1325-1334). A model that uses
  7190 of 7200 s leaves the `accept` tests 10 s, and they time out as a
  rejection.

Change:
- Pass `--timeout` per run and drop llm-kit capability 1.
- State the shared-budget rule. Either emit `timeout_s = model budget +
  acceptance budget`, or have acceptance report `timed_out` as a distinct
  blocked reason ("acceptance starved") rather than a model failure.

### 6. should -- Cut the llm-kit interrupt/`waiting` leg of the blocked flow for v1

Sections: "Prompt and result contract" (lines 212-216), "Blocked flow end to
end" steps 3 and 8, decision 7.

Evidence:
- v1 never answers or resumes an interrupt (lines 230-232). The task folder is
  the block's truth.
- Writing a `task-blocked` interrupt only creates a nonterminal `waiting` run
  that Re-check must later reject. If the user resolves in the harness and
  never presses Re-check, it waits forever.
- It also adds a second failure surface ("failure to write either side rejects
  the attempt") and a dependency on `bootstrap_lib.interrupt_contract` call
  shapes (jobs/interrupts.py:70-110).

Change:
- On a `blocked` result, the acceptance program records the block in the task
  folder and the receipt, then exits with a distinct nonzero code. The ledger
  ends `rejected`, with the typed reason in contract stdout.
- Re-check becomes validate-only. Keep the interrupt as a later increment, if
  continue-with-answer is ever built.

### 7. should -- Cut `jobs events --after-seq`; read ledgers with the existing read-only store

Section: the llm-kit capabilities list, item 3 (line 290).

Evidence:
- llm-kit already ships a read-only store with `snapshot()` (jobs/store.py:2862-2910)
  and `list_events` (store.py:2537).
- Per-run ledgers hold one job, so the full event list is small, and the server
  can filter by seq itself.

Change:
- Drop llm-kit capability 3. That leaves one llm-kit change (cancel; see
  finding 8).

### 8. should -- Process control belongs to the runner's owner; timeouts orphan tool processes

Sections: the llm-kit capabilities list, item 2 (lines 288-289), "Concurrency
and crash recovery", Increment 3.

Evidence:
- The ledger records no process identity: no PID or owner columns in
  jobs/store.py. So `llm-kit jobs cancel` cannot find "the process tree" of a
  run another process drives without a further frozen-format change the design
  does not list.
- llm-kit's own timeout kills only the CLI parent (adapters/claude_runner.py:190-192,
  `proc.kill()`), not its tool subprocesses or sub-agents. On Windows, a test
  or build launched by the worker keeps editing the shared checkout after the
  job is recorded failed.
- With psutil, killing the root first loses the tree on Windows, because there
  is no reparenting and the children of a dead PID are not enumerable.

Change:
- The task-kit runner that spawned the work owns the process tree. On cancel,
  timeout or terminal, it snapshots `psutil.Process(pid).children(recursive=True)`
  BEFORE killing, then kills leaves first and verifies each kill by PID plus
  create time. On Windows, prefer assigning the runner to a Job Object with
  kill-on-close.
- llm-kit adds only a ledger-level `cancelled` terminal transition, plus its
  state, event and migration.
- Add an Increment 3 red check: a worker that spawns a sleeping grandchild,
  then is timed out, leaves no live grandchild.

### 9. should -- Crash recovery kills healthy runners; decouple runner lifetime from the server

Sections: "Concurrency and crash recovery" (lines 150-155), "Server discovery
..." (lines 81-89).

Evidence:
- Runners are separate processes that own their own ledger and receipt. When
  only the server crashes, a detached runner on Windows keeps working.
- The design nevertheless cancels any "dashboard-owned nonterminal runner found
  after a crash". That throws away good work and paid calls.
- The same coupling is why restart must refuse while jobs run (lines 82-84).
  During development of the dashboard itself, that blocks code reloads for up
  to two hours.

Change:
- Record runner PID plus create time in `dashboard.sqlite3` at spawn. On server
  start, a live runner whose identity matches is re-adopted and observed. A
  dead runner with a nonterminal ledger goes to the crash-inspection block.
- With runners independent of the server, a code-hash restart is safe while
  jobs run. Keep the refusal only for `stop --force`-class actions, if any.

### 10. should -- Runtime root ignores the claudx data root; development can touch the production dashboard

Section: "Server discovery, port, and code identity" (line 72).

Evidence:
- The runtime root is hard-coded to `~/.claude/plugins/data/plugins-kit/task-kit/dashboard/`.
- A claudx session sets `CLAUDE_BOOTSTRAP_DATA_ROOT`, and the shims refuse
  production venvs under it (root CLAUDE.md, "Scoped exception: Python CLI
  launcher shims").
- A dev launcher would still find the production `server.json`, see a
  code-hash mismatch, and restart or refuse against the user's production
  instance. That defeats "uninterrupted while you are developing this".
- The marketplace name is also hard-coded (bootstrap reads it from
  marketplace.json; commit b7ac90a9).

Change:
- Derive the root from `${CLAUDE_BOOTSTRAP_DATA_ROOT:-~/.claude/plugins/data}/<marketplace>/task-kit/dashboard/`.
- Add a test that a redirected launcher never reads the production `server.json`.

### 11. should -- The task-mutation lock is one-sided; say so and narrow acceptance accordingly

Section: "Non-interference with harness task use" (lines 304-305).

Evidence:
- The doc says "Every task mutation takes a cross-process lock". The existing
  task.py writers take none (task_system/state_ops.py:195-196 plain
  `write_text`). Neither does the harness agent's Edit tool, nor the worker
  agent, which edits plan.md and log.md with its own tools.
- So the lock only serializes dashboard writers. Safety against harness edits
  rests on comparing the revision immediately before `os.replace`. On Windows,
  `os.replace` can also raise PermissionError while another process holds the
  file open.
- Done-acceptance (lines 209-211) must not treat in-harness edits to OTHER
  items of the same plan.md as the worker's doing, or as a failure.

Change:
- Reword the claim: "dashboard writers serialize on a lock; harness writers
  are detected by revision".
- Define the done check over the target item only: the item is absent, and a
  new log entry names it. Other items' changes are allowed.
- A replace error is a loud 409-class error with no retry.

### 12. should -- There is no path from a blocked task back to job-ready

Sections: "Job-ready contract", item 2 (line 160), "Blocked flow" steps 8-10.

Evidence:
- A block sets task status `blocked` (line 183). `task.py work` has no status
  precondition and does not change status (task_system/state_ops.py:10-17).
- After an in-harness fix, the task therefore stays `blocked`, and job-ready
  item 2 ("stored status is active") refuses every later Queue, including for
  sibling items.
- Nothing in the design restores `active`.

Change:
- Re-check sets task status back to `active` (under expected revision, with a
  log line) when no item is `blocked-user` and validation is clean.
- State that sibling items in a blocked task are not schedulable until then.

### 13. should -- Worker prompt includes `task work` init, which mandates orchestrate and escapes the sub-agent limits

Sections: "Prompt and result contract" (line 203: "task work initialization
context"), "Orchestrator and sub-agents" (lines 264-267).

Evidence:
- The work verb always emits `Skill(awesome-kit:orchestrate)` and directs the
  agent to delegate every implementation unit through it
  (plugins/task-kit/skills/task/SKILL.md:118-121; task-kit bootstrap.json
  `$comment`).
- Orchestrate can dispatch `codex exec` or background agents from the shell.
  Those are not native Agent calls, so the depth-1, three-concurrent and
  six-launch limits and `TASK_KIT_AGENT_DEPTH` do not see them.

Change:
- Either exclude the orchestrate directive from the worker prompt and replace
  it with the bounded native sub-agent rules, or count orchestrate dispatch
  inside the limits. Then add `codex exec` and `claude -p` to the depth-1
  refusal and the Claude deny floor.

### 14. should -- `/task dashboard` collides with the existing `review` verb's routing

Section: "Detached launch and browser open" (line 102).

Evidence:
- The `review` verb already lists the keyword `task dashboard` and opens an
  HTML task overview (plugins/task-kit/skills/task/SKILL.md:204-213).

Change:
- Remove `task dashboard` from review's keywords in the same change that adds
  the capability.
- State whether review's HTML stays separate or the dashboard reuses
  `review_html.py`.

### 15. should -- Inventory polling re-runs git-backed validation every two seconds

Sections: "Awareness, logs, and history" (line 274), "Semantic state" (line 119).

Evidence:
- Validation shells out to `git status`, `git check-ignore` and `git ls-files`
  per folder (task_system/validate.py:135, 165, 184).
- Polling every 2 s over every configured project's tasks means hundreds of git
  spawns per minute. Spawns are slow on Windows.

Change:
- Cache the inventory per task folder, keyed by (path, size, mtime_ns) of its
  `*.md` and `task.yaml`.
- Recompute validation only on change or on an explicit action. The snapshot
  revision is the cache generation.

### 16. should -- Increment order runs autonomous jobs before the safety they need

Section: "Increment plan, ordered by user value".

Evidence:
- Increment 2 already writes task folders (blocks, Re-check) and runs
  unattended jobs. But expected-revision mutation and harness holds arrive in
  Increment 4, and tree kill arrives in Increment 3.
- The user will be using the harness concurrently from Increment 2 onward.
- No increment carries a non-interference test, although that is the top
  binding ruling.

Change:
- Move the mutation helper (revision compare, atomic replace) and the
  scheduler-side task hold (finding 1) into Increment 2. Move runner-owned tree
  kill on timeout (finding 8) into Increment 2.
- Add Increment 1 tests, each shown red first:
  - an import-graph guard that `task.py` and `task_system/` never import
    `dashboard` or `llm_kit`;
  - an assertion that a dashboard refresh leaves every task folder
    byte-identical (hash before and after);
  - byte-identical stdout and stderr for existing verbs with the dashboard
    running.

### 17. should -- Decide the publication-hold mechanism with its cost stated

Section: "Packaging and publication" (lines 329-335).

Evidence:
- A root CLAUDE.md hold on task-kit is a sanctioned mechanism (root CLAUDE.md,
  "A publication hold on ONE plugin belongs here"). But publish.py holds whole
  plugins only (`held_back_for`, scripts/publish.py:207-211).
- Holding "task-kit's dashboard changes" therefore means no task-kit release at
  all, task-skill bug fixes included, until the hold lifts. Otherwise every
  publisher has to use `--only` minus task-kit.
- The doc presents the hold as if it held only the dashboard files.

Change:
- State this cost in the doc. Then choose between two options:
  - accept the freeze;
  - develop the dashboard in a `published: false` sibling plugin, which the
    projection holds back structurally, and fold it into task-kit at release.
- Either choice needs the user's ruling, because it trades skill-fix cadence
  against the "task-kit owns the launcher" layout.

### 18. should -- Specify how `task-kit` reaches the user's terminal PATH

Section: "Components" (lines 57-58).

Evidence:
- Claude Code adds a plugin's `bin/` to PATH for its sessions
  (plugins/hue-kit/README.md:34-35).
- The ruling has the user "typing task-kit", and the Afterplay model it cites
  installs a `~/.local/bin` link through bootstrap `path_entries`
  (research/production-queue.md, section 6.1).
- The design never says which shells see `task-kit` on Windows, macOS and
  Linux.

Change:
- Declare the PATH mechanism in task-kit's bootstrap.json (bootstrap supports
  `path_entries`; bootstrap_lib/path_check.py) or document the in-Claude-only
  scope.
- Add a test per OS in Increment 6.

### 19. nit -- The sixth launcher shim must join the root CLAUDE.md exception list and guard

Section: "Packaging and publication" (lines 336-337).

Evidence:
- Root CLAUDE.md enumerates "the five Python-invoking `plugins/<name>/bin/`
  shims".
- `tests/repo-scripts/test_launcher_shim_data_root_redirect.py` is the guard.

Change:
- Name both edits in Increment 6.

### 20. nit -- Codex flags already belong to llm-kit and bootstrap_lib

Section: "Codex worker" (lines 245-248).

Evidence:
- `windows.sandbox="unelevated"` is emitted unconditionally on Windows
  (bootstrap_lib/codex.py:322).
- The empty `-o` file already raises (adapters/codex_backend.py:1-18).
- task-kit builds no codex argv.

Change:
- Reword as "verified emitted by the shared builder". task-kit supplies only
  `extras.add_dirs` and `extras.network` through `Job.options`.

### 21. nit -- Include the imported task_system modules in the code hash

Section: "Server discovery ..." (line 77).

Evidence:
- The server imports discovery and validation in place (line 62). A change
  there leaves a stale server reused.

Change:
- Add those modules to the hash manifest.

### 22. nit -- Name the acceptance program's interpreter

Section: "Emitted one-job file" (line 197).

Change:
- Emit the contract as `[<absolute sys.executable of the runner>, <absolute
  acceptance script>, ...]`, never a bare `python`. This follows the Python
  interpreter variables rule in root CLAUDE.md.

### 23. nit -- Idempotency keys are redundant for v1

Section: "UI and API mapping" (line 361).

Change:
- `expected_revision` already rejects replays on a single-user loopback API, so
  drop idempotency keys for v1.

### 24. nit -- Use CREATE_NO_WINDOW, and characterize `--allowedTools`

Sections: "Detached launch" (line 95), "Claude worker" (line 236).

Evidence:
- DETACHED_PROCESS alone lets console grandchildren (`cmd /c codex.cmd`) open
  windows.
- Under bypassPermissions, `--allowedTools` is an approval list rather than a
  restriction (inferred; untested). Only the deny list constrains.

Change:
- Add CREATE_NO_WINDOW to the Windows spawn flags.
- Fold the `--allowedTools` question into the existing headless
  characterization test.

## Verified as correct

- The contract receives the model's final text on stdin
  (jobs/run.py:415-417), so parsing a `task_result` in task-kit's acceptance
  program works for both Claude and Codex.
- A contract that exits 0 after writing a valid interrupt request yields
  `waiting` (jobs/model.py:278-280, 322-330). This is accurate, though finding 6
  recommends not using it in v1.
- Workspace isolation defaults to false and the job's declared directory is the
  model cwd (jobs/model.py:47-54, jobs/run.py:1113-1117). `max_attempts`
  defaults to 1 (model.py:590). Job options carry `allowed_tools`,
  `disallowed_tools` and `extras` (model.py:88-98).
- The Claude path emits `--permission-mode bypassPermissions` plus an optional
  `--disallowedTools` (adapters/backends.py:714-728).
- The Codex path defaults to `-s workspace-write`, refuses relative `-C`, and
  forwards `add_dirs` and `network` through extras
  (adapters/codex_backend.py:275-285).
- llm-kit has no cancel verb today (cli/jobs.py:71-143), and its timeout is
  run-wide with a 900 s default (cli/jobs.py:74, jobs/run.py:69). The gap is
  real.
- Existing task code does not import llm_kit. The only cross-plugin import is a
  lazy llm_scripting_kit import in task_system/summary_ops.py:71,93. At the
  code level, the published task skill is therefore unaffected by the optional
  edge.
- The new item field `accept` is safe for the published validator, because the
  schema engine permits unknown keys (skills_kit_lib/schema_engine.py:7) and
  TASK_ITEMS_SCHEMA lists no forbidden keys (task_system/schemas.py:85-106).
  Items authored with `accept` do not break in-harness `validate`.
- The item vocabulary already includes `blocked-user`, and the task vocabulary
  includes `blocked` (task_system/types.py:41-43). No new state is introduced.
- llm-kit is `published: false` (plugins/llm-kit/.claude-plugin/plugin.json),
  and psutil is already a declared repository dependency (llm-kit `local`
  extra). Declaring psutil through task-kit's pyproject and check_imports is the
  correct bootstrap path.
- One ledger per run correctly avoids llm-kit's unsupported
  multi-process-per-ledger case (research/llm-kit-jobs.md, section 1.4).
- Afterplay's launchers are POSIX-only (research/production-queue.md, section
  6.3). Replacing them with port 0, a state file plus PID and create-time
  identity, a health probe, and `os.startfile` is correct and avoids
  `lsof`/`ps`.
- Polling over SSE, a loopback bind, a fragment token, a header token, an Origin
  check, and no CORS fit a local single-user tool.
- Prototype A's actions (Queue, Run next, Cancel run, Retry, Defer/Undefer,
  Re-check, Resolve in Claude Code, Queue all available, Create task, Add only /
  Add and queue, pause, Claude/Codex, effort, timeout, Needs you) all map to
  endpoints. The chips become one `All projects` dropdown, as ruled.
- No worktrees, no branches, no automatic commits, and no OpenRouter top-level
  worker all hold, consistent with the rulings and root CLAUDE.md.
- design/dashboard-architecture.md is ASCII-clean.

## Round 2

Re-review of the revised design/dashboard-architecture.md (498 lines),
checked against its "Revision log", its "Disagreements with review" section,
and the cited source.

### Verdict

APPROVE-WITH-CHANGES. Blocking findings 1 to 4 are resolved, and the
architecture is approved. The "Packaging and publication" section is not
approved until the user rules on finding 17 (see N3). N1, N2, N4 and N5 are
should-level changes; none of them needs another round.

### Blocking findings 1 to 4

- **Finding 1: RESOLVED.**
  - The hold now covers one task folder. The harness always proceeds (lines
    332-338), and the scheduler yields (job-ready item 8, line 178).
  - Decision 5 no longer limits harness work.
  - A test exists (`test_dashboard_harness_hold.py`, Increment 2), and the
    Increment 4 red check covers "task B while task A runs".
- **Finding 2: RESOLVED.**
  - Codex network now defaults to false (lines 144, 267), so `git push` and
    `publish.py` cannot reach a remote.
  - Codex is REFUSE until a live Codex-side deny test passes (lines 267-270).
    That test is a named Increment 2 test with a red check.
  - Caveat: if no Codex-side deny mechanism turns out to exist, Codex stays
    refused. That reduces the ruling's "claude and codex" to Claude only, so
    take it back to the user rather than relaxing the gate.
- **Finding 3: RESOLVED.**
  - The floor now prevents commits instead of trying to attribute them (lines
    257-260). It covers commit, push, reset, rebase, stash, clean, restore,
    checkout, switch, branch, worktree and add.
  - Acceptance requires an unchanged branch and start HEAD as an ancestor of
    end HEAD. The porcelain delta is recorded as history, not used as a gate
    (lines 229-232).
- **Finding 4: RESOLVED.**
  - The floor now lives only in `Job.options.disallowed_tools` (lines 257-258),
    and the premise is downgraded to CONFIRMED WITH CONSTRAINTS (lines
    455-458).
  - Re-verified: only the run floor reaches `_require_floor_subjects`
    (jobs/run.py:1026-1031). The job-level value is merged without subject
    arming (run.py:774-806).
  - Nit: the revision log says "argv tests required", but no increment names
    one. Add to the Increment 2 red checks: "the emitted Claude argv carries
    bypassPermissions plus every floor pattern; the same floor placed at
    JobFile level is refused".

### Finding 7: ruling on the disagreement

The author is right about the evidence, and the outcome still stands.

- `JobStore.__init__` calls `self._migrate()` even when `create=False`
  (jobs/store.py:740-763).
- `_migrate` opens a pragma-writing connection through `_connect`
  (store.py:782-792, 817-829).
- `jobs events` uses `JobStore(..., create=False).list_events` (cli/jobs.py:361-366).
- Only `LedgerReader` opens a connection "that cannot write, even by pragma"
  (store.py:2850, 2879-2880, 2910).

My round-1 evidence wrongly placed `list_events` on the read-only reader. The
recommendation to add no tail flag is unaffected. The correction does create
N1.

### New defects in the revision

- **N1 (should). Reading events from a live ledger breaks the one-process-per-ledger rule.**
  - Lines 310-311 say the server "filters existing ledger event output", and
    Increment 3 adds "existing-event filtering".
  - The only existing path is `JobStore`, which opens a migrating,
    pragma-writing connection. Opening it from the server while the runner
    owns the ledger is the multi-process case the design says it avoids
    (line 139).
  - `RunSnapshot` carries no events (jobs/model.py:1392-1406).
  - Change: while a run is live, show `LedgerReader.snapshot` plus the runner
    log tail and receipt. Read `list_events` only after the runner has exited.
    Or, if live events are needed, add an additive read-only
    `LedgerReader.list_events` to llm-kit. That changes no persisted byte.
- **N2 (should). The `/task work` handshake has no failure contract.**
  - The design says "corrupt state is an error, not absence" (line 81). A
    corrupt `server.json`, or a hung or unreachable server, could therefore
    stall or fail the published `/task work`. That would break the top ruling.
  - Change: the handshake has a short bounded timeout. Any dashboard fault
    prints one loud stderr line naming the fault and the file, then
    `/task work` proceeds unchanged.
  - Add a red check: a corrupt `server.json` and a non-responding port each
    leave `/task work` output byte-identical apart from the notice line.
- **N3 (blocking until the user rules). Option (a) as written contradicts recorded default (3).**
  - Lines 340-344 ship the server, UI, translator and `/task work` handshake in
    published task-kit "as they are built", with no hold.
  - Recorded default (3) says "dashboard/job pieces stay unpublished until
    llm-kit is declared stable". plan.md's extraction consequence says the
    same: "publish the extraction now, dashboard in later versions".
  - This option (a) is also not either of the round-1 options: accept the
    freeze under a root hold, or use a `published: false` sibling.
  - Change: until the user rules, the operating assumption must be consistent
    with default (3), either the root hold or the sibling plugin. If the user
    picks "ship as built", record that ruling as superseding default (3) in
    plan.md before any dashboard code reaches `dev`.
- **N4 (should). Under option (a), consumers get an unrunnable install hint.**
  - The `install: "manual"` llm-kit entry (line 346) would reach every
    task-kit consumer while llm-kit is `published: false`, so it is absent
    from marketplace.json.
  - bootstrap's manual-plugin output names `claude plugin install
    <plugin>@<marketplace>` (bootstrap manifest-reference.md:1242-1246), a
    command that cannot succeed for an unlisted plugin.
  - Change: add the entry only when llm-kit is published. Until then, the
    REFUSE diagnosis says "llm-kit is not yet published" instead of giving an
    install command.
- **N5 (should). The PATH launcher copy in the data directory escapes the shim refusals.**
  - Lines 61-63 put a version-independent launcher bundle in task-kit's data
    `bin/` on PATH.
  - The cached-copy refusal under `CLAUDE_PLUGIN_TEST` checks plugin-cache
    paths only (root CLAUDE.md, launcher-shim exception), so a claudx session
    that types `task-kit` runs the production bundle.
  - The doc also does not say whether the bundle carries the dashboard code or
    locates the installed version.
  - Change: state which one. Extend the claudx refusal to the production
    data-dir bundle, and add that case to
    `test_launcher_shim_data_root_redirect.py` with the Increment 6 red check.

### Verified in round 2

- The revised text is ASCII-clean, and every round-1 nit (19 to 24) is
  reflected.
- `path_entries` writes the Windows User PATH on Windows and an rc line on
  POSIX (bootstrap manifest-reference.md:1034-1040). The PATH mechanism is
  real on all three OSes.
- The rejected-not-waiting blocked flow, the run-wide timeout with a typed
  `acceptance-starved` block, runner-owned tree stop with a Job Object, PID and
  create-time re-adoption, a redirected runtime root, and Re-check
  reactivation are all consistent with the round-1 evidence.
