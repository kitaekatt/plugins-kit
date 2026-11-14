# Proposal: autonomous agent runs from databench

Status: proposal, to be converted into a task. Drafted 2026-11-14 from a design
discussion with the user. Decisions marked **(ruled)** were made by the user in
that discussion; everything else is a recommendation the task may revisit.

Spans three repositories:

- **katsoft-libs** (`packages/katsoft-llm-kit`): a new agent-session Claude
  backend for llm-kit jobs, branch-mode workspaces, a supervisor-owned stdin.
- **databench**: work units, the worktree/branch lifecycle and reaper, merge
  policy execution, live observation, stop/pause/take-over, overhead metrics.
- **plugins-kit**: a project-only `bootstrap run` mode, worktree preparation,
  and the root `CLAUDE.md` worktree rule.

## 1. Goal (v1)

From databench, start work on a unit (a task item or a smaller standalone job).
Databench spawns a headless `claude -p` agent in a git worktree, which works
until it has to stop. While it runs, databench shows that it is running, lets
the user watch it live, stop or pause it, and take it over in a real terminal.
Finished work is committed to a branch and merged into git according to a merge
policy declared when the work was specified.

Out of v1: the "send a message" UI (plumbing only, see 5.4); calendar
scheduling; running on Claude's servers; creating work from databench
(creation stays in-harness, **(ruled)**).

## 2. Decisions

| # | Decision | Source |
|---|---|---|
| D1 | Workers run as `claude -p`. | **(ruled)** |
| D2 | Output and input use stream-json (`--output-format stream-json --input-format stream-json`). | **(ruled)** |
| D3 | Every spawned agent runs in a git worktree, never in a dev checkout. | **(ruled)** |
| D4 | One branch per unit of work (not per run); run branches stay local until merge. | recommended, **accepted** |
| D5 | The branch is the record; the worktree is a disposable copy. No uncommitted change survives a run. | recommended, **accepted** |
| D6 | Worktrees persist >= 48 h after their run ends, then are reaped LRU when a count or storage budget is exceeded. | **(ruled)** |
| D7 | Worktree setup builds on `bootstrap run`; add a project-only mode; record setup timings. | **(ruled)** |
| D8 | Merges go into git refs, never into a working directory. Dev checkouts are updated by hand when stale. | **(ruled)** |
| D9 | Merge is not manually triggered: the work specification declares when to merge. | **(ruled)** |
| D10 | Take-over: databench opens a terminal and starts the harness in the worktree, resuming the session. | **(ruled)** |
| D11 | Databench's existing execution assumptions (M8) may be changed freely. | **(ruled)** |
| D12 | Both task items and standalone jobs are runnable; one execution path serves both. | recommended |

## 3. What exists today (verified 2026-11-14)

Databench M8 (`databench/docs/tasks-execution-plan.md`, complete 2026-10-24):

- SQLite queue `<user cache>/databench/task-execution/execution.sqlite3`, one
  active entry per repository (unique index), states queued -> validating ->
  running -> accepting -> succeeded plus failure states; pause/resume of
  DISPATCH only.
- Manager thread polls every 1 s (`tasks_execution.py`), revalidates revision,
  sessions and consent before dispatch.
- Runner (`tasks_execution_runner.py`) writes `jobs.yaml` and runs
  `llm-kit jobs run` under `llm_kit.jobs.supervisor.start(...)`, with
  `directory = project_root` (the checkout itself) and the `CLAUDE_DENY` list.
- UI: "Run work" in Decide; logs in Trace as 16 KB pages, manual refresh. REST
  polling only, no SSE/websocket.
- Task facts come only from `task-kit export` / `task-kit manage`.

llm-kit jobs (`katsoft-libs/packages/katsoft-llm-kit/src/llm_kit/`):

- `ClaudeCliBackend.complete` (`adapters/backends.py:812-845`) runs
  `claude -p --output-format json --no-session-persistence --permission-mode
  bypassPermissions ...`, prompt on stdin, one invocation per attempt. No
  session id, no transcript, no resume, no streaming.
- `system_prompt_mode` defaults to `replace` (`jobs/run.py:796-799`).
- `jobs/workspace.py`: opt-in `workspace.isolate: true` gives a DETACHED
  worktree per attempt; no branch, no commit, no merge-back; `gc` reaps.
- "Done" is only the job's `contract.command` exiting 0.
- `relay/resume.py` already runs `claude -p --resume <id> --output-format
  stream-json --verbose` under a lease and checks the `init` event's
  session_id/cwd. Reuse its parsing and validation.

**Defect found during this survey:** databench's runner sets
`prompt.system` to a one-line string and does not set `system_prompt_mode`
(`tasks_execution_runner.py:89`), so today's task workers run with Claude
Code's own system prompt REPLACED. Fix independently of this proposal (set
`append`), and adopt `append` as the agent-session default.

Messaging a `claude -p` process through a connector does not work today: the
llm-kit relay is a development channel, Claude Code confirms its load on every
launch, and `clauder --relay` refuses `-p` for that reason
(`plugins/relaunch-kit/README.md:105-111`).

## 4. Work units and the work specification

One execution path, two sources (D12):

- **Task item**: today's M8 source. Long-lived, multi-session; the task
  folder carries progress.
- **Standalone job**: "do this; you are done when <contract> passes". No
  task folder. Specified in-harness (unproven path; v1 can be a YAML file
  written by a small skill or CLI, validated by databench).

Every work specification carries:

| Field | Meaning |
|---|---|
| `repository` | Git repository to work in. |
| `base_ref` | Ref the unit's branch starts from (default: `origin/<default dev branch>`). |
| `goal` | Natural-language goal (job) or task item ref (task). |
| `done_when` | Acceptance command(s); maps to the llm-kit contract. Task items reuse their `accept:` lists. |
| `merge` | Merge policy (section 7). Required; no implicit default for auto-merge into a published branch. |
| `harness`, `model`, `effort`, `timeout` | As in M8 today. |

A task item's merge policy may come from the task (a new `task.yaml` field,
e.g. `merge:`) so it is declared once per task rather than per run. That needs
a task-kit schema change; flag it to task-kit.

## 5. Agent-session backend (katsoft-libs, llm-kit)

Add a second Claude backend for jobs, selected per job (e.g.
`options.session: agent`), beside today's completion backend:

```
claude -p --input-format stream-json --output-format stream-json --verbose
  --model <m> --append-system-prompt <system> --permission-mode bypassPermissions
  --disallowedTools <deny> [--session-id <uuid> | --resume <id>]
```

### 5.1 Session identity

- Session persistence ON (no `--no-session-persistence`).
- Record `session_id` (from the `init` event, or pre-assigned with
  `--session-id`) and the cwd on the attempt row. A new ledger column; bump the
  ledger schema.
- The cwd is the unit's fixed worktree path (6.2), which is what makes a later
  `--resume` find the transcript.

### 5.2 Event stream

- The supervisor writes the stream-json stdout to `events.jsonl` in the run
  directory, verbatim and line-buffered. This is the observation source; it
  replaces the 16 KB stdout pages for agent runs.
- The final `result` event supplies the response text the contract reads.

### 5.3 stdin ownership

- The supervisor (not the jobs runner's transient child) holds the stdin pipe
  for the life of the process and feeds it from a per-run mailbox (an
  append-only file or a local socket under the run directory).
- The initial prompt is the first user message on stdin.
- v1 writes to stdin only for control: stop at the end of the turn
  (5.5). The mailbox format is designed so that "send a message" is a later UI
  feature with no library change.

### 5.4 Messaging (plumbing in v1, UI deferred)

Primary: a user message written to the mailbox is forwarded to stdin as a
stream-json user message and processed when the current turn ends.

Candidate upgrade, to verify before choosing: a `PostToolUse` hook reads the
same mailbox and returns new messages as `hookSpecificOutput.additionalContext`,
which delivers mid-turn between tool calls. Verify that `-p` stream-json runs
honour that hook output.

### 5.5 Stop, pause, take-over semantics

- **Stop**: interrupt at the end of the current turn (stream-json control
  request if supported, else close stdin after the turn), then the end-of-run
  commit (6.1). The session remains resumable.
- **Pause**: the same as stop, plus the unit is marked `paused` so the
  dispatcher does not restart it; resume = `--resume <id>` in the same
  worktree. Never SIGSTOP a worker (in-flight API calls time out).
- **Hard cancel**: the existing supervisor process-tree cancel, as a
  fallback when the turn does not end.

To verify: whether the stream-json input protocol accepts an interrupt control
message in `-p` mode, and the exact queuing behaviour of a user message sent
mid-turn.

## 6. Branches and worktrees

### 6.1 Branch model (D4, D5)

- One local branch per unit: `agent/<repo-unit-id>` (task: per task, so
  successive runs continue it; job: per job).
- Created from `base_ref` at first dispatch; later runs reuse it.
- **End-of-run commit**: when a run ends for ANY reason (accepted, blocked,
  failed, stopped, timed out), the runner commits every remaining change in the
  worktree to the unit branch (a `WIP: <unit> run <n>` commit if the agent left
  changes uncommitted). Afterwards the worktree is always clean.
  - Gitignored content is not committed and is regenerable by setup (6.3).
  - If the commit itself fails (e.g. hook rejection), the worktree is marked
    `dirty-held` and is exempt from reaping until a human resolves it.
- A branch is deleted only when merged (section 7) or explicitly discarded.
  Unmerged branches with no live run are shown in databench as "needs a
  decision".
- Run branches are not pushed; the merge pushes the result (7.2).

### 6.2 Worktree lifecycle (D6)

- Path is FIXED per unit: `<worktree root>/<repo>/<unit-id>`. Recreating a
  reaped worktree from its branch at the same path restores `--resume`.
- States: `active` (run live) -> `parked` (run ended; retained) ->
  `reapable` (>= 48 h since the run ended) -> removed. `dirty-held` is outside
  this flow.
- Reaper (a databench service, runs periodically and before each dispatch):
  - Budgets: `max_worktrees` and `max_bytes` (configurable).
  - While either budget is exceeded, remove the least recently used
    `reapable` worktree.
  - If budgets are exceeded and nothing is `reapable`, remove nothing and raise
    a visible warning; dispatch continues.
  - Before removing: assert `git status --porcelain` is empty and HEAD equals
    the unit branch tip; otherwise mark `dirty-held` instead.
  - Never deletes a branch.
- Size measurement excludes the shared object store (a worktree's `.git` is a
  file); it includes venvs and `node_modules`, which are likely to dominate.
- Re-hydration: dispatching a unit whose worktree was reaped recreates it at
  the same path from the branch and re-runs setup.
- The llm-kit `workspace` module gains a branch mode (create/reuse a named
  branch at a fixed path, no per-attempt uuid path) or databench owns worktree
  creation and passes `directory`; pick one owner. Recommendation: llm-kit
  owns creation (it already has the per-repo `worktree add` lock and ledger
  records), databench owns retention and reaping policy.

### 6.3 Worktree preparation (D7)

- After `worktree add`, run setup in the worktree via `bootstrap run`.
- Add a **project-only mode** to `bootstrap run` (e.g. `--project-only`):
  apply only the `<project>/.claude/bootstrap.json` layer and project venv,
  skipping marketplace refresh, plugin updates and plugin manifests. Not a
  blocker; v1 may use the full pass.
- Gitignored links a checkout depends on must be recreated. Example:
  plugins-kit's `dev/tasks` is a gitignored symlink into the private tasks
  repo, so a fresh worktree has no task folders and `/task work` fails.
  Recommendation: express such links in the project bootstrap layer (bootstrap
  already has a link check, `agent_skills_check.py`), so the same mechanism
  serves dev checkouts and worktrees.
- **Overhead metrics**: record, per run, the durations of `worktree add`,
  project setup, and time to the first `init` event; and the worktree's size
  after setup. Show them in databench and keep them in the execution store so
  trends are visible.

## 7. Merge policy (D8, D9)

### 7.1 Policy values

Declared in the work specification (section 4):

| Policy | Behaviour |
|---|---|
| `on_accept` | Merge into `target` as soon as the run is accepted (contract passes). |
| `on_unit_done` | Task only: merge when the task closes (all items done), not per item. |
| `never` | Keep the branch; it appears under "needs a decision". |

Plus `target` (branch, e.g. `dev`) and `remote` (default `origin`).

### 7.2 Mechanics (no working directory involved)

1. `git fetch <remote> <target>`.
2. `git merge-tree --write-tree <remote>/<target> <unit branch>`; on conflict,
   mark the unit `merge-conflict` (needs a human), do not retry automatically.
3. `git commit-tree` the result with both parents.
4. `git push <remote> <sha>:refs/heads/<target>`; a rejected non-fast-forward
   means the target moved: go back to step 1 (bounded retries).
5. On success, delete the unit branch and mark the worktree `parked`.

**Do not update the LOCAL `refs/heads/<target>`.** Worktrees share the
repository with the dev checkout, where `<target>` is usually checked out.
Moving that ref under a checkout makes its index and tree look like a reversal
of the merged work, and the next commit there would revert it. Push to the
remote only; dev checkouts pull when the user updates them (D8).

### 7.3 Consequence in plugins-kit

In plugins-kit, pushing to `dev` consents to the next publish carrying the work
(root `CLAUDE.md`, "The implied contract"). An `on_accept` merge into `dev`
therefore makes agent work publishable without further review. The merge
policy in the specification is the control point; the task should decide
whether plugins-kit specifications may use `on_accept` into `dev`, or merge
into an integration branch instead.

## 8. Databench changes

- **Execution store**: unit (task item or job), unit branch, worktree path and
  state, session_id, run directory, timings, merge state. Relax "one active
  entry per repository" to a per-repository concurrency limit (worktrees remove
  the shared-checkout reason for it).
- **Live view**: render `events.jsonl` (assistant text, tool calls, results)
  as a running transcript; tail by byte offset with auto-refresh (polling is
  acceptable for v1; SSE optional).
- **Controls**: Stop, Pause/Resume (5.5), Take over (below), Cancel (hard).
- **Take over** (D10): stop at the end of the turn, mark the unit `taken-over`
  (dispatcher skips it), then open a terminal running
  `claude --resume <session_id>` with cwd = the worktree. Launch is per OS
  (macOS: `osascript` to Terminal/iTerm; Windows: `wt.exe`; Linux: a
  configured terminal command). The user hands back with a databench action
  that clears `taken-over`.
- **Branches and worktrees panel**: budgets and usage, parked/reapable list,
  `dirty-held` and `merge-conflict` items, unmerged branches needing a decision.
- **Remove** the M8 assumptions this replaces: checkout-as-directory, "the
  manager never commits", one active entry per repository.

## 9. Permissions

- Keep `bypassPermissions` plus the deny list; the worktree narrows the blast
  radius.
- Add `Bash(git push*)` to the deny list: only the manager pushes (7.2).
- Workers must not run `git worktree` or `git checkout`/`switch` (deny), so the
  fixed path/branch invariant holds.

## 10. Rules this proposal changes

- plugins-kit root `CLAUDE.md`, "Worktrees and scratch copies": "a worktree is
  only ever a READ-ONLY copy". Needs an explicit carve-out for
  databench-managed agent worktrees (D3), or a rewrite.
- `docs/planning/task-kit-dashboard/architecture.md:861-862` rejected
  "worktree per run" and automatic agent commits; this proposal reverses both.
  Record the reversal there.
- databench `docs/tasks-execution-plan.md`: one active run per repository;
  "the manager never commits arbitrary code automatically".
- `plugins/CLAUDE.md` "Plugins take no position on worktrees" applies to
  shipped plugin text; check that any task-kit or llm-kit skill text written
  for this does not steer users toward worktrees.

## 11. Suggested milestones

1. **Fix the system-prompt defect** (databench sets `system_prompt_mode:
   append`). Independent, small.
2. **llm-kit agent-session backend**: stream-json in/out, session id on the
   attempt, `events.jsonl`, supervisor-owned stdin, stop-at-turn-end. Tests in
   katsoft-libs; release; import into plugins-kit `data/package-targets.json`.
3. **Branch-mode workspaces** + end-of-run commit.
4. **Databench**: run on worktrees, live view, stop/pause, take-over terminal.
5. **Reaper and budgets**, overhead metrics.
6. **Merge policy** execution and the work-spec `merge` field (task-kit
   schema change for task-level policy).
7. **Standalone jobs** from an in-harness specification.
8. **`bootstrap run --project-only`** and project-layer link declarations
   (can land any time after 3).
9. Deferred: message UI on top of the 5.3 mailbox.

## 12. To verify before or during implementation

- stream-json input in `-p` mode: interrupt control message support; queuing
  of a user message sent mid-turn.
- `PostToolUse` `additionalContext` delivery in `-p` stream-json runs (5.4).
- task-kit working-session registration from a worktree: the `checkout` field,
  and `TASK_KIT_AGENT_DEPTH` (registration is skipped when it is set).
- presence-kit hooks are inert unless `CLAUDE_CODE_SESSION_ATTENDED=1`;
  confirm `claude -p` workers do not set it, and that a taken-over interactive
  resume does.
- Per-worktree venv and `node_modules` sizes in the user's repositories, to
  size `max_bytes`.
- Whether an approved (non-development) channel can be loaded in `-p`; if so
  it is an alternative messaging route to 5.4.
