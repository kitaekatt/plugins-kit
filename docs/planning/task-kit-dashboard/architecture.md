# Task Dashboard Architecture
Status: design only; plan checkpoint for `dashboard-architecture`. Scope: a machine-wide task dashboard owned by task-kit, with autonomous work provided through an optional llm-kit edge.
## Binding rulings
The following words are requirements, not prompts for reinterpretation:
> we should have a robust system where a task can be blocked and for now the
> way of resolving that will be to switch to the task in-harness to resolve
> that issue.
> I'd prefer to just block the task and fix it if there is a problem
> translating it to a job, vs trying to make a job system that is so robust
> it can handle any worded task.
> We also only need to support claude and codex for working on tasks for now
> (we don't need to consider openrouter endpoints as valid entities to work
> on tasks, though they should be able to use endpoints for agent works).
> When you get to the UX stage I'll suggest you make two html prototypes
> ... Building the actual UX before we get an approved prototype would be a
> waste of time.
> I'm imagining the user launches the dashboard by typing task-kit. See how
> afterplay launches interfaces i.e. afterplay-web and follow that model.
> typing /task dashboard in claude should also run 'task-kit' as a
> convenient way of launching it
> the user should be able to continue to use the task system in the claude
> code harness uninterrupted while you are developing this. This should be
> free and automatic as the dashboard runs in parallel to the task system
> versus changing its foundation
> don't copy patterns you consider poor this production system is a cool
> prototype and it inspired this but always follow engineering best
> practices as indicated in the plugins-kit repo
The selected UX ruling is:
> A is good enough I'd suggest the project filter is just a dropdown
The recorded defaults also bind this design, quoted without reinterpretation:
> Grill round 2 (Q6-Q8, 2026-10-17): no objection given, recommendations stand as defaults: (6) unattended Claude workers keep bypassPermissions plus a disallowed-tools floor (force-push, reset --hard, deletes outside the checkout, publish.py); (7) v1 resolution = fix in harness, then a fresh job run on the next item; (8) llm-kit (unpublished) may change its frozen formats for a per-job timeout and `jobs cancel`.
> Grill round 1 (2026-10-17): user gave no objection to the recommendations, so they stand as defaults: (1) task-kit depends on awesome-kit for orchestrate; (2) publish the extraction now, dashboard in later versions; (3) dashboard/job pieces stay unpublished until llm-kit is declared stable; (4) one autonomous task per project, run in its checkout, parallel across projects; (5) one machine-wide dashboard over all projects, with a project filter.
> Consequence for the extraction: the skill's name changes from awesome-kit:task to task-kit:task. Publish both in ONE release and install task-kit in the same bootstrap pass, so there is no gap with no task skill. The dashboard only reads task folders and adds verbs/state; it does not change the existing verbs' contracts.
## Goals and non-goals
- Show every configured project's task folders and open items in one browser.
- Queue one task item as one durable, observable Claude or Codex job.
- Preserve task folders as the only truth for task and item meaning.
- Make translation, execution, cancellation, and recovery fail loudly.
- Keep existing task.py verb contracts usable when the dashboard is absent.
- Support Windows, macOS, and Linux without `lsof` or `ps`.
- Do not add a DAG, a second task model, automatic commits, releases, branches,
  worktrees, or a web framework.
- Do not use OpenRouter or other transport endpoints as top-level task workers.
## Ten load-bearing decisions
1. task-kit owns the launcher, HTTP server, UI, translation, and acceptance.
2. llm-kit remains an optional REFUSE edge used only by autonomous run actions.
3. Task folders are semantic truth; a task-kit SQLite database is runtime truth.
4. Each run has one flat llm-kit job and its own schema-owned SQLite ledger.
5. One autonomous project lease permits one running item; harness work is not limited.
6. Workers edit the shared checkout but never branch, worktree, commit, or publish.
7. A block becomes task `blocked`, item `blocked-user`, and llm-kit `rejected`.
8. Resolution happens in `/task work <ref>`; Re-check may reactivate; later runs are fresh.
9. The browser polls versioned snapshots and cursor-based tails; v1 has no SSE.
10. Prototype A is retained, with its project chips replaced by one dropdown.
## Component and process model
### Components
- `plugins/task-kit/bin/task-kit` and `task-kit.cmd` are venv-first launchers.
  They implement launch, `status`, `stop`, and `restart`; no argument launches.
- `plugins/task-kit/bin/task_kit_entrypoint.py` finds code beside the shim and
  enters the dashboard launcher without depending on the caller's cwd.
- Bootstrap syncs a version-independent launcher bundle to task-kit's data
  `bin/` and adds that directory through `path_entries`; POSIX shell files and
  Windows User PATH therefore expose `task-kit` outside Claude Code too.
- Dashboard modules live beside the current task system under
  `plugins/task-kit/skills/task/scripts/task_system/dashboard/`; existing
  task-system modules are imported in place and are not relocated.
- One detached dashboard server owns discovery, the API, the runtime database,
  project scheduling, and reconciliation.
- One child runner process exists for each active project. A runner owns one
  run and one llm-kit ledger, then exits; it is never a daemon.
- Static HTML, CSS, and JavaScript implement prototype A without a UI framework.
### HTTP stack decision
Use `http.server.ThreadingHTTPServer`, `sqlite3`, `json`, and other standard library modules. The API is small, local, and JSON-only; FastAPI/Uvicorn would add provisioning and lifecycle surface without buying required behavior. Declare `psutil>=6` in task-kit's `pyproject.toml` and `bootstrap.json`, but only for cross-platform PID identity, ancestor liveness, and process-tree shutdown. This is preferable to platform command parsing and is already a repository-used dependency. Every new dependency is provisioned by bootstrap, never ad hoc.
### Server discovery, port, and code identity
- Runtime root is `${CLAUDE_BOOTSTRAP_DATA_ROOT:-~/.claude/plugins/data}/`
  `<marketplace>/task-kit/dashboard/`; marketplace identity comes from the
  resolved manifest/bootstrap context, never a hard-coded `plugins-kit` string.
- The server holds an OS file lock on `server.lock` for its whole lifetime.
- On a fresh start the server binds `127.0.0.1` port 0, then atomically writes
  `server.json`: schema, PID, PID creation time, instance nonce, port, token,
  code hash, start time, and log path. Corrupt state is an error, not absence.
- The code hash is SHA-256 over an explicit manifest of dashboard Python,
  static assets, imported task-system discovery/validation modules, and
  task-kit's version. It excludes tasks and runtime data.
- The launcher reads state and probes `/api/v1/health`. PID identity is accepted
  only when PID plus creation time and instance nonce match the health response.
- A matching healthy server is reused. A code-hash mismatch may restart the
  server while detached runners continue; the new server re-adopts each runner
  only when its recorded PID and creation time match, then resumes observation.
- A dead runner with a nonterminal ledger becomes a crash-inspection block;
  queued records survive and are reconciled before scheduling.
- Stop requests authenticated graceful shutdown. Only an explicit force-stop
  may stop a live runner, and it uses the runner-owned tree-stop protocol.
- No port-owner discovery is attempted. A missing/corrupt ownership record never
  licenses killing an arbitrary listener.
### Detached launch and browser open
- The server starts with stdin detached and stdout/stderr appended to
  `dashboard/server.log`; the launcher retains its own stderr crash path.
- POSIX uses a new session. Windows includes `CREATE_NO_WINDOW` in its hidden,
  detached process flags so console grandchildren cannot flash windows.
- Readiness is the health API, polled for at most 30 seconds; timeout reports the
  log path and exits nonzero.
- Windows opens the URL with `os.startfile`; macOS runs `open`; Linux runs
  `xdg-open`. Spawn errors, nonzero exit, and timeout are distinct failures.
- The launcher always prints the URL. `TASK_KIT_NO_OPEN=1` and `--no-open`
  suppress browser opening without changing server behavior.
- `/task dashboard` is a skill-level capability that runs `task-kit`; it is not
  a fifteenth task.py verb. Adding it removes `task dashboard` from `review`'s
  keywords; the existing `review_html.py` report remains a separate snapshot.
## Local HTTP security
- Bind only `127.0.0.1`; reject other Host values and emit no CORS allowance.
- Open `http://127.0.0.1:<port>/#<token>`; the fragment never reaches server logs.
- JavaScript sends the token in `X-Task-Kit-Token` for every data/API request.
- Mutations additionally require same-origin `Origin`, JSON content type, and a
  64 KiB body cap. Every response is `Cache-Control: no-store`.
- Serve a restrictive CSP: self scripts/styles, no objects, frames, or remotes.
- Logs redact tokens, common credential shapes, and prompt bodies by default.
- Health reveals only instance, code hash, port, and busy count; no task data.
## State and source of truth
### Semantic state
- `task.yaml` remains the task-level source of truth.
- The one `task_items` block in `plan.md` remains the item source of truth.
- Completion still means removing the item, adding an Accomplished line, and
  appending history to `log.md`; no `done` item state is introduced.
- Dashboard scans call the existing task-system discovery and validation code.
- Inventory is cached per task folder by each `*.md`/`task.yaml` tuple of path,
  size, and `mtime_ns`; validation reruns only on a change or explicit action,
  and the cache generation is the snapshot revision.
- A refresh is read-only. Merely running the server never edits a task folder.
### Runtime state
- `dashboard.sqlite3` is machine-scoped runtime state, owned by the one server.
- It stores canonical projects, queue entries, run mappings, project leases,
  interactive harness holds, runtime events, run options, and history indexes.
- Project identity is SHA-256 of the canonical project root; display names are
  projections, not identity.
- Each run directory is
  `dashboard/projects/<project-id>/runs/<run-id>/` and contains the emitted job
  file, llm-kit ledger, acceptance scratch, redacted runner log, and receipt.
- One ledger per run avoids llm-kit's unsupported multi-process-per-ledger case.
- Task folders never contain queue rows, PIDs, ports, model settings, or ledgers.
- Runtime records keep task ref, item id, and a source revision; they do not
  override a changed or missing task item.
### Configuration seams
Use `~/.claude/task.local.yaml` for user/machine choices already in that domain: `dashboard.max_parallel_projects` (default 2), `default_timeout_s` (7200), Claude/Codex model entry defaults, effort defaults, Codex network (false), `dashboard.endpoint_helpers` (empty; see Endpoint helpers), and `dashboard.subagents` (`enabled` true, `max_concurrent` 3, `recheck_days` 7; see Orchestrator and sub-agents). Per-queue model, effort, network, and timeout override these defaults, are disclosed in the UI, and are stored only in runtime state.
These seams pass the plugin-opinion razor because concurrency, wall time,
subscription choice, network access, and external-provider use each carry a
real cost. Loopback binding, one active item per project, no automatic commits,
and explicit Retry remain hard requirements rather than preferences.
## Concurrency and crash recovery
- Queue order is FIFO within a project; `Run next` changes only that project.
- Start order is fair across projects: the walk is `(newest started_at of the
  entry's checkout, or "" if it never started; that checkout's oldest queued seq;
  seq)`, so the checkout that started a run least recently goes first and
  checkouts that tie keep first-come order. The page's "Queued #n" is that same
  walk (`Scheduler.queue_view`).
- A tick starts at most one run: the first entry whose attempt changes its state
  (starts, fails, is skipped or blocked) ends the tick, and the scheduler wakes
  itself while another entry can start. An attempt that leaves its entry queued
  does not end the tick, so one stuck head cannot starve the others. Bound:
  Cancel and Remove wait on the tick lock for one start (translation plus the
  runner handshake), not for the whole queue.
- A queued entry that cannot start carries a waiting reason shown beside its
  place: `config`, `llm-kit-unavailable`, `held`, `checkout-busy`, `queued-behind`
  (an earlier entry of its checkout goes first), `no-slot`, or `cli-check`
  (a new Claude binary is being observed). The start loop's pre-check and the UI
  share one function (`Scheduler.wait_reason`).
- A translation block is refused while a Claude Code session holds the task or
  any other dashboard run works its folder (409 `held` / `run-active` inside the
  gate; Queue answers 422 `translation-failed` with `blocked: false`, and a start
  leaves the entry queued). A run's own job-file block is the one write that
  ignores the run that owns it: after the lease the run is STARTING and so
  active, and only a hold or ANOTHER active run refuses.
- A database transaction acquires one project lease before a runner is spawned.
- The global bound admits at most `max_parallel_projects`; idle projects do not
  consume a slot.
- Physically duplicated task folders reached through links are assigned once by
  existing discovery deduplication, so two project queues cannot own one item.
- Queue/start re-read the task under a task mutation lock and compare revision.
  A block the dashboard records for a translation failure carries the revision
  that translation read: a harness fix that landed meanwhile is a 409 at Queue
  and leaves the run queued at start, never a block on stale text.
- Lock order, outermost first: `Scheduler._queue_lock` (Queue, Queue all, every
  authoring write), `_tick_lock` (a tick, Cancel, Remove, Save run options, every
  authoring write, so no run starts between an authoring check and its write),
  the mutator's cross-process file lock (waits up to 10 seconds), then
  `_hold_gate`. The gate is a plain lock held only for a short check-and-commit:
  a write's `precommit` checks (live hold, active run, the item's live run) plus
  its per-file compare and `os.replace`, the start's hold re-check plus
  `try_lease`, and hold registration. Nothing under the gate waits on a lock or
  runs a process, so hold registration, which takes only the gate and never
  `_tick_lock` or the file lock, fits the 2 second handshake budget even while a
  tick runs for 20 seconds or another process holds the file lock.
- Every server-process writer is gated: Re-check, the three translation blocks,
  crash inspection and acceptance-starved blocks, and every authoring write. Only
  the runner process's acceptance block cannot (another process; it takes the file
  lock) and `init` for a brand-new folder needs none. An AST guard keeps new
  writers gated. Re-check releases only the holds that predate it: registration
  bumps a per-folder epoch and Re-check's `precommit` refuses (409 `held`) when
  it moved since the request began.
- A queued run starts through a cheap pre-check (the checkout holds a lease, no
  slot is free, or the folder is held: skipped without translating, since
  translation runs git), then translation outside the gate, then the gated hold
  re-check and lease. A hold that registers between translation and the lease
  stops the start; one that registers after it sees the active run.
- A changed but still job-ready item is translated again; stale prompt text is
  never run. A no-longer-ready item follows the blocking path.
- Runners record PID plus creation time in dashboard state before dispatch.
- On server start, matching live runners are re-adopted; terminal ledgers are
  reconciled, while dead nonterminal runners become crash-inspection blocks.
- The runner owns its process tree. On cancel, timeout, or terminal it snapshots
  descendants before stopping them leaves-first and verifies PID/create-time;
  Windows runners require a kill-on-close Job Object assigned before dispatch.
- No crashed or failed run auto-retries. Partial shared-tree work must be
  inspected in the harness before a fresh run.
- Queued entries survive a server crash and resume only after reconciliation.
## Task item to job translation
### Job-ready contract
An item is job-ready only when all of these are true:
1. `task validate <ref>` has zero errors and zero warnings.
2. The task's stored status is `active`.
3. The named item exists exactly once and its state is `available`.
4. `plan.md` contains exactly one actionable section keyed by that id.
5. The selected model entry resolves to adapter `claude-cli` or `codex-cli`.
6. The project, task folder, contract script, and writable roots resolve.
7. The optional llm-kit frontier probe passes before any ledger is created.
8. No interactive hold exists for this task folder; holds never cover a project.
An item section is an ATX heading at level 2 through 4 whose first token is the
exact item id, normally `### <id> -- <title>`. Its body ends at the next heading
of equal or higher level and must contain non-whitespace text. Missing or
duplicate sections are errors; fuzzy title matching is forbidden.
Add one optional item field:
```yaml
accept:
  - ["uv", "run", "pytest", "tests/example.py", "-q"]
```
`accept` is a list of non-empty argv lists, runs from the project root, and is
never shell-interpreted. Absence means structural acceptance only. Do not add
`unattended`, model, effort, timeout, or commit fields: Queue is the unattended
authorization, and the other values describe a run rather than the task.
### Fail-loud translation
- Translation returns either an immutable job specification or typed reasons.
- On any reason, no llm-kit job file, run, reservation, or ledger is created.
- When the item block is safely editable, atomically set item state
  `blocked-user`, put the concise code/detail in `note`, set task status
  `blocked`, and append one dated log entry.
- If malformed source prevents safe item mutation, set task status `blocked`
  when task.yaml is writable and record the full reason in task history; the UI
  must say that the item could not be mutated because its source was invalid.
- Failure to record the block is itself a nonzero API response naming the file;
  runtime state must never pretend the task folder was updated.
### Emitted one-job file
- The JobFile contains one Job and `max_parallel: 1`.
- Job id is a stable task/item stem plus a unique run id; retry creates a new id.
- `directory` is the project root and `workspace.isolate` is false.
- `max_attempts` is 1; UI Retry creates a fresh run instead of replaying writes.
- The run option is passed through `jobs run --timeout`; with one Job it is the
  job budget. Model and contract share it, and zero contract remainder records
  typed `acceptance-starved` blocking rather than a model failure.
- `models` contains exactly the chosen Claude or Codex registry entry.
- The contract argv starts with the runner's absolute `sys.executable`, then the
  absolute acceptance script, task ref, item id, run id, start revision, start
  HEAD, and branch; it never names an interpreter by its bare command word.
### Prompt and result contract
The system prompt names the project root, task ref, item, permissions, allowed
writes, no-commit rule, sub-agent limits, block procedure, and acceptance
schema. The user prompt includes the exact item entry, exact detail section,
required task context and reads, and current source revision. It excludes the
ordinary `task work` orchestrate directive and supplies bounded native-agent
rules instead.
It does not copy unrelated future items into the prompt.
The final answer must contain exactly one `task_result` JSON object with:
`schema`, `status` (`done` or `blocked`), `summary`, `needs`, `reason`,
`resume_hint`, `checks`, and `changed_paths`. Missing, duplicate, or malformed
results are rejection, never inferred from prose.
For `done`, acceptance checks only the target item: it is absent and a new log
entry names it. Changes to sibling items are allowed. Task validation and every
optional `accept` argv must pass. The branch must be unchanged and start HEAD
must remain an ancestor of end HEAD; intervening commits are observed, never
attributed. Start/end `git status --porcelain` and their path delta are history,
not gates. The receipt records all outcomes.
For `blocked`, acceptance atomically records task `blocked`, item
`blocked-user`, reason and resume hint, writes the receipt, and exits with a
distinct nonzero code. The ledger ends `rejected` with the typed reason in
contract stdout; v1 creates no interrupt and has no waiting/resume leg.
## Blocked flow end to end
1. Translation failure blocks immediately and creates no job.
2. A running agent reports `blocked` only through the structured result.
3. Acceptance updates the task folder and receipt; the ledger rejects the run.
4. Dashboard renders `Needs you`, reason, need kind, resume hint, and log tail.
5. `Resolve in Claude Code` shows copyable `cd <project> && claude` and
   `/task work <task-ref>` instructions; it does not fake terminal launch.
6. The running project lease is released before the item is offered to harness.
7. The user fixes or finishes the item through the ordinary task skill.
8. `Re-check` releases the task hold and validates. Under expected revision it
   restores task status to `active` with a log line only when validation is clean
   and no item is `blocked-user`; otherwise the whole task remains unschedulable.
9. If the item was removed, history records resolved/completed in harness.
10. If it is `available`, the next Queue action creates a new run and ledger.
V1 never answers or resumes an llm-kit interrupt; the binding default requires
a fresh run after in-harness resolution.
## Permissions and allowed writes
### Claude worker
- Run `claude -p` with `--permission-mode bypassPermissions`.
- Characterize `--allowedTools` live as an approval list, not a restriction.
- Put the tested floor only in `Job.options.disallowed_tools`, never JobFile or
  `jobs run`; it denies git commit/push/reset/rebase/stash/clean/restore/checkout/
  switch/branch/worktree/add, publication, destructive deletes, `claude -p`,
  `codex exec`, `task-kit`, `llm-kit jobs`, `llm-kit pipeline` and
  `llm-kit completion`. Path-inexpressible delete rules deny the command family.
- The same single comma-joined argument carries tool-level denials after the Bash
  patterns: `Workflow`, `EnterWorktree`, `RemoteTrigger`, `CronCreate` and
  `ScheduleWakeup` in every run, and `Agent` while native sub-agents are off (a run
  has them on only under a current characterization record; see Orchestrator and
  sub-agents). These are Claude Code's documented deny channel
  (`--disallowedTools` takes the same rule syntax as `permissions.deny`); a name a
  later CLI does not offer is reported by the observation below, not an error.
  `translate.worker_job_options` is the one builder of the value.
- Every run's environment sets `CLAUDE_CODE_DISABLE_WORKFLOWS=1` (documented;
  turning workflows off also makes ultracode unavailable) and
  `TASK_KIT_AGENT_DEPTH=0` (the marker below). `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=0`
  is not used: the CLI ignores a zero. A run with native sub-agents ON adds
  `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1` and `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=<max_concurrent>`
  and keeps every other denial and the workflow switch.
- Refuse launch if the installed Claude CLI cannot represent the floor; do not
  silently drop a deny rule. The CLI is observed once per binary (real path, mtime,
  size) per server life, on a server-owned thread that never runs under a
  scheduler lock: the worker argv llm-kit would emit is captured through the edge
  (`JobsEdge.claude_worker_argv`), then a baseline run (the floor minus every
  tool-level name) and an actual run (the byte-identical value a run emits) read
  the CLI-authored `system`/`init` tool list and stop, and a third run asks the
  model to run `git commit --dry-run` and must show a CLI-authored permission
  error plus a `permission_denials` entry. States: `observed`; `ignored` (a listed
  tool is still offered, or the command ran), which refuses Claude runs
  (Queue 422 `bad-options` with reason `cli-deny-ignored`; an entry already queued
  ends `failed`, the task untouched); `vacuous` (no sub-agent tool in the
  baseline); `unobserved`. The last two start runs with the denials, which are the
  documented control, and the UI says what was not observed. A Claude start for a
  binary not yet observed waits (reason `cli-check`) for at most the observation
  (about 110 seconds), once per binary; Cancel never waits on it.
- The observation spawns the `claude` binary three times per new binary (two
  stopped at the init event, one haiku call) from a scratch directory under the
  dashboard root with an empty strict MCP config and `--setting-sources local`.
- Each run's `spec.json` records `subagents` (the run's decision: state, code,
  reason, limits, the denied tools) and the observation summary.
### Tripwires, not confinement
Every nested-run guard is a tripwire against a cooperating or careless worker, the
same class as the deny floor under `bypassPermissions` (grill ruling 6). None of
it is confinement.

| Guard | Kind | Stops | Does not stop |
| --- | --- | --- | --- |
| `TASK_KIT_AGENT_DEPTH` marker + launcher refusal | tripwire (our code) | `task-kit ...` run with the inherited environment | `env -u`, an emptied environment |
| launcher ancestry refusal | executable-level guard (our code) | `env -u`, absolute paths, `bash -c`, the `/task dashboard` form `bash <root>/bin/task-kit` | a double fork that reparents before the check; calling the dashboard API with the token in `server.json`; importing dashboard code |
| deny floor prefixes (git, deletes, publish, `claude -p`, `codex exec`, `task-kit`, `llm-kit jobs/pipeline/completion`) | tripwire (Claude CLI rules) | the plain command forms | wrappers (`bash -c`, `env`, `xargs`), absolute paths, interpreters running inline code, tools whose own subprocess starts a CLI |
| tool denials `Agent` (while off), `Workflow`, `EnterWorktree`, `RemoteTrigger`, `CronCreate`, `ScheduleWakeup` in the one comma-joined floor argument | documented CLI control, the exact argument observed per binary | those tools | nothing known |
| `CLAUDE_CODE_DISABLE_WORKFLOWS=1` (every run) | documented CLI control | dynamic workflows; ultracode | nothing known |
| `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1`, `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=<max_concurrent>` (ON runs only) | documented CLI controls, per session | nesting; more than `max_concurrent` sub-agents at once | a resumed finished sub-agent takes a slot unchecked (documented); total launches (no CLI cap since Claude Code 2.1.224); a server-side flag that can skip the at-once check (binary evidence, undocumented) |
| helper broker (`endpoint_helper.py`) | enforced by the server | widening a run's offers or budgets, an unconsented helper, forged or edited run state, a call after the run ended | direct endpoint calls by importing the model library or `curl` with keys readable in the user's data directory |
| `floor.check_worker_models` at dispatch | second gate behind translation | a job naming a transport (text-only) entry as a worker | n/a |
| Codex workers | refused until the Codex floor is proved | all Codex runs | n/a |

The launcher refuses every command (exit 6), `status` and `serve` included, when
`TASK_KIT_AGENT_DEPTH` is present or when one of its ancestors is a live runner of
the dashboard it targets: an ancestor whose command line names `runner_cli.py` with
a run directory under `<root>/projects/`, whose PID and creation time equal that
directory's `runner.json`. An isolated dashboard (`--data-dir`) has another root and
is not refused, so task-kit's own tests can run inside a worker. Subprocess
environments are built explicitly (`floor.run_environment`, tests'
`dashboard_support.clean_env`); nothing deletes the marker globally.
### Codex worker
- The shared builder, not task-kit, must be verified to emit workspace-write,
  absolute `-C`, nonempty `-o`, absolute add-dirs, and Windows unelevated mode.
  Task-kit supplies only `extras.add_dirs` and `extras.network` through Job options.
- Network defaults false; opt-in is disclosed per run. Codex is additionally
  REFUSE until a live Codex-side policy test denies the same git/publication/
  delete/nested-worker floor as Claude; llm-kit's dropped disallowed-tools field
  is never presented as enforcement.
- Require a non-empty `-o` result file and structured result validation.
### Shared write policy
Workers may edit files inside the selected project and selected task folder,
and may write their assigned run scratch. They may run relevant tests.
They may not manipulate the index, create/switch branches or worktrees, commit,
push, publish, rewrite history, or destructively replace shared-tree work. The
host applies the worker floors above; it never resets or repairs a breach.
This follows plugins-kit's shared-tree discipline and keeps review/publication
with the user in the harness.
## Orchestrator and sub-agents
- The one top-level job is the Claude or Codex orchestrator and owns the result.
- Claude uses its native Agent tool; Codex uses its native spawn-agent tool.
- Native sub-agents are OFF unless a current characterization record turns them on
  (Claude only; Codex workers are refused). Off means the `Agent` tool is denied
  with the workflow, worktree, remote and scheduled-agent tools (see Claude
  worker), and the worker's prompt says so. A missing, stale or failed record never
  refuses a run: the run proceeds with the tool denied and the page says why, with
  the command that fixes it.
- `task-kit characterize [--worker claude] [--entry NAME]` runs three real Claude
  sessions with the exact worker argv llm-kit emits and the ON deny list (every
  denial but `Agent`), changing only the output format to stream-json so events can
  be read, and judges them from CLI-authored telemetry alone: `tool_use` and
  `tool_result` blocks, `task_started` events (which carry `spawn_depth`),
  `permission_denials`, and files the sessions did or did not create. The model's
  answer is never evidence, and a missing event is `inconclusive`, never a pass.
  Leg A (depth limit 1) asks the orchestrator for a harmless command, a denied
  `rm`, the helper client's `--check` and one child that writes a file, runs its own
  denied `rm` and tries to launch a grandchild. Leg B (depth limit 1, one at once)
  asks for two launches in one message. The control leg repeats A with no depth
  limit. Checks: `agent_callable`, `floor_orchestrator`, `floor_inherited`,
  `depth_cap`, `concurrent_cap`, and the informational `helper_command_allowed`.
  The command prints every check, writes the record whatever the verdict and exits
  0 (1 when it cannot run, 2 for `--worker codex`, 6 inside a run).
- On Claude Code 2.1.304 a child at the depth limit is not offered the sub-agent
  tool at all, so there is no CLI-authored refusal to read; `depth_cap` therefore
  needs the control: it passes when the child started at `spawn_depth` 1, no
  grandchild started, and the same request without the limit launched a grandchild
  (`task_started` at `spawn_depth` 2). A CLI refusal that names the nesting limit
  is accepted as evidence on its own. The concurrency limit does arrive as a
  refusal (`Concurrent subagent limit reached`). Sub-agents are asynchronous in
  `claude -p` (the launch returns at once and the child reports after the
  orchestrator's first result), so a leg is read to the end of the stream.
- The record (`<dashboard root>/subagents.json`) is bound to the binary's SHA-256
  digest and `--version`, to the characterization's `PROBE_VERSION`, and expires:
  ON ends at the earlier of the record's own `expires_at` and `checked_at` plus
  `recheck_days` (default 7), so a shorter setting acts at once and a longer one
  does not extend a record already written. Evidence is the probe directory under
  `<dashboard root>/characterize/<stamp>/`. A record that exists but cannot be used
  is named corrupt, never read as absent. `subagents.decide` is the one place the
  question is answered: Codex off; `enabled: false` off; a binary the per-binary
  denial check saw ignore a denial refused (that refusal is the Claude worker
  section's, whatever the record says); no, corrupt, stale-digest, stale-version,
  other-probe-version or expired record off; any gating check not `pass` off;
  otherwise ON.
- ON is bounded only by Claude Code's documented per-session controls: depth 1
  (children launch, grandchildren cannot) and `max_concurrent` (default 3) at once,
  set in the run's environment. The workflow switch and every denial but `Agent`
  stay, and a sub-agent has the orchestrator's permissions and the same denied
  commands. The prompt then tells the agent the limits (true to the decision) and
  that it alone reports the task result.
- Disclosed residuals, shown in the page and in capabilities under ON: a resumed
  finished sub-agent takes a slot without the at-once limit being checked
  (documented); there is no limit on the total number of sub-agents (Claude Code
  removed its per-session cap in 2.1.224); and Claude Code can lift the at-once
  limit from its servers (binary evidence, undocumented, which is why ON expires).
  Closed by a documented control: ultracode, which is exempt from the limit, is
  unavailable because every run switches workflows off.
- A re-observation every Nth start is not done: it needs a paid multi-minute session
  that launches sub-agents, a run cannot be observed passively (llm-kit's Claude
  transport reads only the final JSON envelope), and the open residuals vary per
  session. What recurs cheaply is the per-binary denial observation, the per-run
  documented environment and the expiry.
- The earlier limit of 6 child launches per job is dropped: Claude Code has no
  control for it since 2.1.224, and enforcing it would need a per-run PreToolUse hook
  and an llm-kit change. The run timeout and the at-once limit bound the work.
- If characterization fails, the orchestrator works alone and the UI discloses that
  native sub-agents are unavailable; it never substitutes another top-level model.
- `TASK_KIT_AGENT_DEPTH` is set to 0 in a run's environment. In-process
  sub-agents share the orchestrator's environment, so the variable cannot differ
  for children: it marks a dashboard run's process tree, and the launcher refuses
  where it is present (a tripwire; see Tripwires, not confinement). Nested
  `task-kit`, `llm-kit jobs`, `llm-kit pipeline`, `llm-kit completion`,
  `codex exec` and `claude -p` are in each worker's deny floor.
- Configured OpenRouter/local endpoints are available to a run only as endpoint
  helpers, brokered by the dashboard server (next section). A helper is never a
  worker.
### Endpoint helpers
An endpoint helper is a text-only model (no tools, no file access) a run may ask a
question. It never owns acceptance, never writes the checkout and is never a
top-level worker: the worker allowlist stays claude and codex, helper names
`claude` and `codex` are refused at configuration, a transport entry as a worker is
refused by translation and again by `floor.check_worker_models` at dispatch, and
the helper route accepts only a live run's token.

Configuration (`~/.claude/task.local.yaml`, `dashboard.endpoint_helpers`; default
none). Each helper is a name (`^[a-z][a-z0-9-]{0,31}$`) with `models` (registry
entry ids), `repositories` (consent), an optional one-line `purpose` (200
characters), `max_calls` per run (default 20), `timeout_s` per call (default 300)
and `max_input_chars` (default 100000, at most 1000000), which counts the input and
the system text together. A shape error names the file.

Consent is by repository identity, never by display name (display names collide on
purpose). The key is the `origin` URL normalized (scheme and user dropped, host
lowercased, a default port dropped, `git@host:org/repo` becomes `host/org/repo`,
`.git` and trailing slashes stripped, the repository path keeping its case); a
checkout with no `origin` is keyed `path:<realpath>` and listed in `repositories`
by its absolute path. A helper is offered to a run only when its key is listed. A
helper whose models include a harness entry (an agent) refuses a run offered it:
Queue answers 422 `bad-options` with reason `endpoint-helper`, a queued entry ends
`failed` `bad-options`, and the task is untouched. A model the registry does not
know is not refused at Queue; the call that names it ends `unroutable` and says
why.

All authority is server-side. The worker can write every file in its run
directory, so nothing in it decides anything:
- Two additive runtime tables (the schema stays 1; an older task-kit opens the
  database, checks only `meta.schema` and never reads them). `helper_grants` holds,
  per run, the SHA-256 of the run's token, the offers (helpers, models, limits) and
  when the grant was revoked; `helper_calls` holds one row per call or refusal
  (helper, state, entry, status, input characters, output characters, usage,
  reported cost, detail). The offers never change after dispatch.
- Dispatch issues the grant for a run offered at least one helper and puts only
  two variables in its environment: `TASK_KIT_RUN_TOKEN` (the token) and
  `TASK_KIT_DASHBOARD_ROOT` (where `server.json` lives). A run offered nothing
  gets neither. The token is registered with the log redactor.
- Revocation: a terminal `RuntimeDB.set_state` is one `BEGIN IMMEDIATE` holding the
  run's state update, the grant's revocation and the event; releasing a starting
  run to the queue revokes too. A server start marks `reserved` calls with no end
  `interrupted`; they still count (fail closed).
- Route `POST /api/v1/helper-calls/<helper>`, authenticated only by the
  `X-Task-Kit-Run-Token` header (checked before the dashboard token, which
  authorizes nothing here; the run token authorizes nothing else). Host is checked as
  everywhere; a request that carries an `Origin` header is refused (a page cannot
  cause egress); the body cap is 4 MiB on this route and 64 KiB elsewhere. Body:
  `{"input": str, "system": str | null}`. Answers: 401 unknown token, 410 revoked or
  the run is no longer active, 404 helper not offered (lists the offered ones), 422
  empty or malformed or oversize request ("input plus system is N characters; this
  helper accepts M"), 429 budget spent ("n of m calls for <helper>"). Every
  refusal after the token is recognized writes a `refused` row.
- Authorize and reserve are one step: `RuntimeDB.reserve_helper_call` runs, in one
  `BEGIN IMMEDIATE` under the connection lock, the token, the revocation, the run's
  liveness, the offer, the request size and the per-helper budget (reserved,
  completed, failed and interrupted rows count), then inserts the `reserved` row.
  Revocation is a transaction under the same lock, so a reservation is either
  committed before the revocation or refused 410. Budgets are per helper.
- The call: `JobsEdge.complete_transport` runs llm-kit's `declaration.run` with the
  guarantees `filesystem-write`, `shell-exec` and `subagent-spawn` (a transport entry
  has no tools; a harness entry is armed text-only), one attempt per model, in the
  request thread, from the run's checkout. The answer is
  `{status, entry, text, attempts, usage, reported_cost_usd}`; a floor (no usable
  entry) is `unroutable` with every entry's disposition. The raw input, the
  caller's system text and the output are kept privately at
  `<run dir>/helpers/<call id>.in.txt`, `.system.txt` and `.out.txt` and are never
  served.
- Client: `helper_cli.py`, standard library only, run as `"<python>" "<path>"
  <helper>` with the excerpt on stdin. It reads the two environment variables,
  finds the port in `server.json` on each call and prints the answer. Exit 0
  answered; 1 the call failed or the server cannot be reached; 2 refused; 3 no
  usable endpoint. `--check` prints a marker without a server.
- The worker prompt names the offered helpers and the command only when a helper is
  offered. The page shows "Endpoint helpers for this repository: <names> | none
  configured" in the run controls and the calls of a run in its detail
  (`GET /api/v1/runs/<id>` gains `helpers_offered` and `helper_calls`).

What this does not stop is in the guard table: a process that calls the model
library directly, or reads an endpoint key from the user's data directory, reaches
an endpoint without a helper.
## Awareness, logs, and history
- Use polling, not SSE: every 2 seconds while visible, immediately after an
  action, and every 15 seconds while hidden. Polling recovers naturally after
  sleep, browser reload, and server restart and matches the ledger snapshot model.
- `GET /api/v1/dashboard` returns one revisioned snapshot with projects, tasks,
  items, queues, active runs, attention records, and recent history.
- `GET /api/v1/runs/<id>/log?after=<cursor>` returns a bounded redacted tail.
- Runtime events include queued, translating, started, model-call-started,
  heartbeat, task-source-changed, blocked, cancelling, and terminal.
- Show real phases and elapsed time. Do not fabricate percentage or step counts.
- History joins the task-kit run receipt to the immutable llm-kit ledger; raw
  prompt/result text stays in the private run directory and is collapsed by default.
- Keep history until an explicit future GC action; v1 performs no silent expiry.
The server caches inventory as described above; no two-second poll revalidates
unchanged folders. While a run is live the server reads ledger state only through
`LedgerReader.snapshot` (read-only connection), plus the runner log tail and
receipt. It reads events through `JobStore.list_events` (which migrates through a
pragma-writing connection) only after the runner has exited, filtering by sequence
itself; this keeps one process per ledger. No llm-kit file is touched by this.
llm-kit adds exactly one generic capability: a ledger-level `cancelled` terminal
state, reason, event, and migration. Task-kit owns process control and passes the
run-wide timeout. This frozen-state revision requires architecture, rollback,
schema/version-fixture, and differential-test updates. Model streaming is deferred:
current ledger events, runner stderr heartbeats, source revision changes, and log
tails are sufficient for v1; changing Claude/Codex transports only for decorative
token-by-token motion is not justified.
## Non-interference with harness task use
- Existing task.py verbs and their stdout/stderr contracts do not change.
- `/task dashboard` is additive; direct task usage never imports dashboard code.
- Discovery and browser refresh write nothing.
- Dashboard writes task folders only for explicit New/Add/Defer/Queue/Re-check
  actions and run-boundary state transitions, all under expected revisions
  (New task: the target folder must be absent).
- Dashboard writers serialize on a cross-process lock; harness writers do not,
  and cannot without changing the task system (Claude Code's Edit and Write and
  hand edits take no lock either). The guarantee is therefore narrower than
  "harness changes are never overwritten":
  - a dashboard authoring, translation-block or Re-check write is refused (409
    `held`) while a `/task work` hold that the write did not account for is live;
    run-boundary writes (crash inspection, acceptance) record what a live run did
    and do not refuse a hold, because the session was told a run was live;
  - each mutation writes and fsyncs every sibling temp first, then, file by file,
    re-reads the target and compares it with the bytes the edit was built from
    immediately before `os.replace`; a harness change that landed before that
    compare is never overwritten (409 `stale-revision`, nothing of that file
    written); a replace error is a loud 409-class response with no retry;
  - the files are replaced one at a time (plan.md, task.yaml, log.md), so a
    failure part-way is not folder-atomic: the response is 409 `partial-write`
    naming the failed file, the files written and the files not written, with
    no rollback;
  - residual, stated so no document claims more: a harness write WITHOUT a hold
    (a task.py verb outside `/task work`, an editor, an agent not using the
    skill) that lands between a file's final compare and its `os.replace`, or an
    append through a handle opened before the replace (an `open("a")` log
    append), is lost and not detected. The window is one read, one digest compare
    and one rename. `test_residual_race_a_harness_write_landing_inside_the_replace_is_lost_and_is_documented_as_such`
    pins that this window exists.
- Runtime/ledger writes stay in plugin data, never beside plugin code or tasks.
- The optional llm-kit import is lazy. Absent, too-old, and stale copies produce
  distinct install/update diagnoses and disable only autonomous run controls.
The skill-level `/task work` flow adds a dashboard handshake before calling the
unchanged work verb. When online, it registers a hold only for that task folder,
keyed by Claude session id, ancestor PID, and creation time. The scheduler yields:
it never starts a held task. An already-live run causes one notice with run id and
cancel instructions, then `/task work` proceeds unchanged. Holds end with the
session or Re-check. With no dashboard, the handshake is a no-op.
Handshake failure contract: the handshake has a short bounded timeout (2 seconds
total). A corrupt `server.json`, an unreachable port, or a hung server never stalls
or fails `/task work`: it prints one loud stderr line naming the fault and the file,
then `/task work` proceeds unchanged with no hold registered. Thus task B can
be worked while task A runs in the same project, without folder-backed runtime state.
## Packaging and publication
User ruling, verbatim: "i am the only user of task-kit it's fine to ship". This is
option (a) and supersedes recorded default (3) for the dashboard: it ships inside
task-kit as built, with no hold and no sibling plugin. Record the ruling in plan.md
before dashboard code reaches `dev`. No root `CLAUDE.md` hold is added.
- task-kit ships its shims, server, UI, translator, acceptance, mutation helpers,
  and `/task dashboard`. Viewing/manual task management works everywhere;
  autonomous Run/Queue controls and endpoints REFUSE loudly unless a compatible
  llm-kit is installed.
- Add `psutil` to `pyproject.toml` and bootstrap `check_imports`; add `llm_kit` to
  `shared_lib_imports`. Add no `install: "manual"` llm-kit plugin
  entry: llm-kit is `published: false`, so bootstrap's `claude plugin install`
  hint cannot succeed and must not reach consumers. Do not add llm-kit to
  task-kit's plugin.json dependencies.
- Job-backed actions are REFUSE units. A run action's refusal (503) names llm-kit
  as not yet published or too old, with no install command; the dashboard still
  renders a truthful read-only/task-management view with an unavailable banner.
- Probe the newest llm-kit cancelled-state frontier symbol and distinguish absent
  from too-old/stale. llm-kit owns only the generic cancelled-state change, no task
  semantics, UI, or task-kit paths, and stays `published: false` until the user
  declares it stable and separately authorizes publication.
- The data-dir `bin/` PATH bundle is a thin launcher: no dashboard code; it locates
  the installed task-kit version at run time.
- The POSIX and Windows shims follow the repository's venv-first order, redirected
  data-root refusal, cached-copy refusal, and launcher-shim tests. Under
  `CLAUDE_PLUGIN_TEST` the cached-copy refusal also covers the launcher copy in
  task-kit's production data-dir `bin/` (the one on PATH), not only plugin-cache
  paths; the guard test covers both.
## UI and API mapping
Prototype A remains the layout: Now running, Needs you, project pipelines,
detail drawer, queue overview, and recent runs. Replace project chips with a
single `All projects` dropdown. Keep the Needs you toggle and text search.
Server-backed actions map as follows:
| UI action | Endpoint |
| --- | --- |
| Load/filter/search | `GET /api/v1/dashboard?project=&q=&needs=` |
| Open item/run details | `GET /api/v1/items/<project>/<task>/<item>` |
| Pause/resume scheduler | `POST /api/v1/scheduler` |
| Queue item | `POST /api/v1/items/<project>/<task>/<item>/queue` |
| Queue all available | `POST /api/v1/tasks/<project>/<task>/queue` |
| Remove queued item | `DELETE /api/v1/queue/<queue-id>` |
| Run next | `POST /api/v1/queue/<queue-id>/front` |
| Cancel run | `POST /api/v1/runs/<run-id>/cancel` |
| Retry failed/cancelled | `POST /api/v1/runs/<run-id>/retry` |
| Defer/undefer item | `PATCH /api/v1/items/<project>/<task>/<item>` |
| Re-check blocked item | `POST /api/v1/items/<project>/<task>/<item>/recheck` |
| Resolve instructions | `GET /api/v1/items/<project>/<task>/<item>/handoff` |
| Save model/effort/timeout | `PUT /api/v1/items/<project>/<task>/<item>/run-options` |
| Add item / add and queue | `POST /api/v1/tasks/<project>/<task>/items` |
| Create task | `POST /api/v1/projects/<project>/tasks` |
| Poll log tail | `GET /api/v1/runs/<run-id>/log?after=` |
Every task-folder write carries `expected_revision`. Three kinds of action do not. New task has no folder
yet, so its precondition is the ABSENCE of the target folder (409 `task-exists`). Cancel run, Remove from
queue and Save run options act on a runtime record only (the task revision moves while a worker runs), so
the server validates the record's own state under the scheduler lock instead (409 `not-cancellable`,
`not-queued`). Authoring is also refused while a session holds the task or a run works it (409 `held`,
`run-active`, `item-has-live-run`). A block carries the revision its translation read. Remove from queue
ends the entry `removed` (not retryable, outside the run history; runtime schema stays 1). v1 has no redundant idempotency key. Stale input is
409 with current revision; validation/translation refusal is 422 with typed
reasons; unavailable llm-kit is 503 with the exact remedy. Selection, drawer
close, copy, disclosure expansion, and form cancel are client-only and have no
endpoint because they alter no shared state.
## Increment plan, ordered by user value
### Increment 1 - visible read-only dashboard
Ship launcher, server, secure static UI, discovery, project dropdown, pipelines,
Needs you, and recent task activity without llm-kit.
Tests: `test_task_kit_launcher.py`, `test_dashboard_http.py`,
`test_dashboard_inventory.py`, `test_dashboard_ui_contract.py`.
Red checks: import graph forbids task.py/task_system importing dashboard/llm-kit;
refresh leaves task folders byte-identical; existing verb stdout/stderr is byte-
identical with the server running. Also fail corrupt state and non-loopback Host.
### Increment 2 - one autonomous vertical slice
Add translation, one selected Claude/Codex job, acceptance/rejected block,
expected-revision atomic mutation, scheduler-side task holds, runner-owned tree
stop on timeout, in-harness handoff, Re-check, and fresh retry for one project.
Tests: `test_task_job_translation.py`, `test_task_job_acceptance.py`,
`test_dashboard_blocked_flow.py`, `test_dashboard_optional_llm_kit.py`,
`test_dashboard_harness_hold.py`, `test_dashboard_process_tree.py`, `test_dashboard_codex_safety_live.py`.
Red checks: missing plan section creates no ledger; timed-out grandchild dies; an unproved Codex floor REFUSES; the emitted Claude argv carries bypassPermissions plus every floor pattern, and the same floor placed at JobFile level is refused (`Job.options.disallowed_tools` argv test); a corrupt `server.json` and a non-responding port each leave `/task work` output byte-identical apart from the one notice line.
### Increment 3 - cancellation and durable recovery
Add llm-kit cancelled state, runner cancel, re-adoption/crash reconciliation,
event reads only after runner exit (live runs use `LedgerReader.snapshot`,
log tail, and receipt; no llm-kit file touched), and durable queues.
Tests: `test_jobs_cancelled_state.py`, `test_dashboard_cancel.py`,
`test_dashboard_crash_recovery.py`, `test_dashboard_runner_readoption.py`.
Red check: kill only the server and retain a healthy runner; kill a runner and
block inspection. Neither case may silently retry or report false completion.
### Increment 4 - safe dashboard authoring
Add New task, Add item, Defer, Queue all, and run options while preserving task.py.
Tests: `test_dashboard_task_mutations.py`, `test_dashboard_optimistic_lock.py`,
`test_task_skill_dashboard.py`.
Red check: same-task Queue yields to `/task work`; work on task B proceeds while task A runs.
As built: New task (dev/tasks only, the task system's own `init`, priority and a first item
in one follow-up write), Add item and add-and-queue (plan.md block entry plus a
`### <id> -- <title>` section at the end of the region holding the block, verified in
memory before any write), Defer/Undefer, Queue all available (one combined block when any
item fails translation, otherwise all-or-nothing enqueue), Remove from queue (`removed`),
and run options (worker, effort, time limit per queue entry; an option problem is a 422 on the
request, never a block on the task). The forms render inline in the redesigned layout; what a
person has typed survives the poll's re-render. Run next is deferred: it needs the only
runtime-schema change of this increment (a queue-rank column, schema 2, which an older
task-kit would refuse), so it is not built. A start walks queued entries in order but skips,
without translating, any whose checkout holds a lease, whose slot is not free, or whose
folder is held.
### Increment 5 - parallel projects and orchestrated sub-work
Add bounded cross-project scheduling, native sub-agent characterization gates,
recursion limits, and configured endpoint helpers for sub-work only.
Tests: `test_dashboard_project_scheduler.py`,
`test_dashboard_headless_subagents_live.py`, `test_dashboard_recursion_guard.py`,
`test_dashboard_endpoint_helper.py`.
Red check: a helper selected as the top-level worker or a depth-2 launch refuses.
Ships as three releases: scheduler fairness, the explicit sub-work deny with its
per-binary observation and the nested-run tripwires first; the endpoint helper
broker second; native sub-agents ON with their expiring characterization last
(`test_dashboard_subagent_gate.py` offline against a fake CLI, `test_dashboard_headless_subagents_live.py`
live with `TASK_KIT_LIVE_CLAUDE=1`).
### Increment 6 - release hardening
Exercise Windows/macOS/Linux launcher behavior, browser failures, security,
optional-edge diagnoses, packaging, bootstrap declarations, and publication choice.
Tests: `test_dashboard_launcher_windows.py`, `test_dashboard_launcher_posix.py`,
`test_dashboard_security.py`, existing launcher-shim/dependency guards, and a
targeted claudx smoke test.
Add task-kit as the sixth Python launcher in root `CLAUDE.md` and
`test_launcher_shim_data_root_redirect.py`; test external PATH reachability on
Windows, macOS, and Linux.
Red check: delete the redirected task-kit venv and assert both shims exit 2 without
falling back to the production venv or reading production `server.json`; a claudx
session that runs the PATH launcher copy from the production data-dir `bin/` exits 2.
Each check is first observed failing against the defect it claims to prevent,
then restored and run targeted. Full-suite work waits for the release gate.
## Rejected alternatives
- Copy Afterplay's journal: rejected because task folders already hold intent.
- Fixed port plus `lsof`/`ps`: rejected as non-Windows and unnecessary.
- FastAPI/Uvicorn: rejected as dependency and lifecycle cost for a local API.
- SSE/WebSocket: rejected for v1 because polling matches durable snapshots.
- One machine-wide llm ledger: rejected by one-process-per-ledger ownership.
- Worktree per run: rejected by explicit shared-tree and plugin-neutral rules.
- Automatic agent commits: rejected because review and publication remain human.
- Resume the blocked model session: rejected; current continuation is contract-only.
- Auto-retry: rejected because repeated writes and paid calls compound failures.
- OpenRouter top-level workers: rejected by the user ruling; helpers remain bounded.
- Option (b), a `published: false` sibling plugin: rejected by the user ruling.
- Hard task-kit dependency on llm-kit: rejected because published task use must live
  independently of an unpublished optional capability.
## Open product questions
None open. New task defaults to `dev/tasks`; endpoint-helper consent is per repository identity (origin URL, or path without an origin).
## Premise outcomes
- CONFIRMED WITH CONSTRAINTS - one item can be one flat llm-kit Job with a shared
  directory and one-prompt/one-contract ledger. The agentic Claude deny floor must
  live in `Job.options.disallowed_tools`, not JobFile/run-level floor; v1 uses
  run-wide timeout, rejected blocks, existing events, and one cancelled-state change.
- CONFIRMED for Claude (live, Claude Code 2.1.304, 2026-10-20) - a headless
  `claude -p` job can call the sub-agent tool and the children inherit the Bash
  floor (a child's denied `rm` was refused and listed in `permission_denials`);
  with `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1` a child launched at `spawn_depth` 1
  and was not offered the tool to launch a grandchild, while the same request without
  the variable launched one at `spawn_depth` 2; with `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=1`
  the second of two launches in one message was refused with `Concurrent subagent
  limit reached`. Sub-agents are asynchronous in `claude -p`. UNTESTED for Codex:
  its native spawn-agent mechanism is not exercised, so Codex workers stay refused.
- CONFIRMED (Claude Code documentation; observed on 2.1.304) - denying the
  sub-agent tool is a documented control (`--disallowedTools Agent`; the tool is
  named `Task` in the init event and `Task(...)` rules alias to `Agent`), and the
  CLI's init tool list shows it removed. `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`
  and `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` are documented (positive integers
  only: a zero is ignored); the per-session launch cap variable is removed.
- REFUTED as enforcement - `TASK_KIT_AGENT_DEPTH`, the deny floor prefixes and the
  helper's environment are tripwires (`env -u`, wrappers, absolute paths bypass
  them); only the launcher's ancestry check is executable-level.
- CONFIRMED - llm-kit's `declaration.run` makes a tool-less completion: the
  `guarantees` requirement admits a transport entry (no tools) and arms a harness
  entry text-only (llm-kit 0.1.5 and later; the dashboard requires 0.3.0). The
  endpoint helper call rests on it, and `jobs_edge` probes its symbols.
- CONFIRMED - published task-kit can host the dashboard behind an optional
  llm-kit REFUSE edge: bootstrap soft-links optional shared libraries, supports
  manual discovery, and existing task modules do not import llm-kit.
- CONFIRMED - Afterplay's launcher is macOS/Linux only. Its source uses `lsof`,
  `ps`, POSIX process groups, no Windows browser command, and Windows bootstrap
  skip; this design replaces those mechanics rather than copying them.

## Disagreements with review

- Finding 7's outcome is accepted, but its evidence calls `list_events` read-only. It is `JobStore.list_events`, and even `create=False` constructs `JobStore` then always migrates (`$DEVROOT/plugins-kit3/plugins/llm-kit/lib/llm_kit/jobs/store.py:740,759-763,817-829,2537`; caller `$DEVROOT/plugins-kit3/plugins/llm-kit/lib/llm_kit/cli/jobs.py:361-366`). Only `LedgerReader.snapshot` is explicitly read-only (`$DEVROOT/plugins-kit3/plugins/llm-kit/lib/llm_kit/jobs/store.py:2850,2910`). V1 still drops the tail flag; live runs use `LedgerReader.snapshot` and events are read only after runner exit (round 2, N1).

## Revision log

- 1 fixed - task-scoped, one-directional holds; harness always proceeds.
- 2 fixed - Codex network defaults off and runs require a live deny-policy gate.
- 3 fixed - prevention floors replace attribution; ancestry and status delta are observational.
- 4 fixed - Claude floor is only `Job.options.disallowed_tools`; argv tests required.
- 5 fixed - run-wide timeout retained; acceptance starvation is a typed block.
- 6 fixed - blocked contracts reject; v1 has no interrupt/waiting leg.
- 7 fixed - tail CLI removed; evidence detail is disagreed above.
- 8 fixed - runner owns verified tree stop; llm-kit owns only cancelled ledger state.
- 9 fixed - matching live runners are re-adopted across server restart.
- 10 fixed - runtime root honors redirected data root and resolved marketplace.
- 11 fixed - lock claim narrowed, target-only acceptance, replace errors are 409/no-retry.
- 12 fixed - clean Re-check reactivates only after all blocked-user items clear.
- 13 fixed - worker prompt omits orchestrate initialization and denies nested drivers.
- 14 fixed - dashboard routing replaces the review keyword; reports stay separate.
- 15 fixed - inventory/validation cache is fingerprinted per task folder.
- 16 fixed - non-interference guards and safety primitives precede autonomous work.
- 17 fixed - user ruling "i am the only user of task-kit it's fine to ship": option (a), no hold, no sibling; option (b) rejected.
- 18 fixed - bootstrap stable data bin plus path_entries exposes every shell.
- 19 fixed - Increment 6 names the sixth-shim guidance and guard edits.
- 20 fixed - shared builder owns Codex argv; task-kit passes add-dirs/network only.
- 21 fixed - code hash includes imported discovery/validation modules.
- 22 fixed - contract argv begins with the runner's absolute sys.executable.
- 23 fixed - v1 idempotency keys removed.
- 24 fixed - Windows uses CREATE_NO_WINDOW; allowedTools is live-characterized.
- 25 fixed (N1) - live runs read `LedgerReader.snapshot`; events only after runner exit; Increment 3 item, no llm-kit file touched.
- 26 fixed (N2) - `/task work` handshake: 2 s timeout, one loud notice, then proceeds.
- 27 fixed (N4) - no manual llm-kit entry; run actions REFUSE naming llm-kit.
- 28 fixed (N5) - claudx refusal and guard test cover the data-dir PATH launcher copy.
- 29 fixed (nit) - Increment 2 red checks include the `disallowed_tools` argv test.
- 30 resolved - New task defaults to `dev/tasks`; endpoint-helper consent is given per project.
- 31 fixed - "harness changes are never overwritten" narrowed: hold refusal, per-file compare, no folder atomicity, the stated residual race.
- 32 fixed - fair start order, one start per tick, waiting reasons; a translation block is refused while a run works the folder; sub-work is explicitly denied (Agent, Workflow, EnterWorktree, RemoteTrigger, CronCreate, ScheduleWakeup) with the exact emitted argument observed per CLI binary off the tick lock; the launcher refuses inside a run (marker and ancestry); guards are tripwires, not confinement.
- 33 fixed - endpoint helpers are brokered by the server: consent by repository identity, offers and per-helper budgets in two additive runtime tables, a run-scoped token revoked in the transaction that ends the run, atomic authorize-and-reserve, size counted over input plus system, and a text-only call made through llm-kit; the run's directory decides nothing.
- 34 fixed - native sub-agents turn ON only under a current characterization record (`task-kit characterize`): bound to the binary's digest and version, expiring after `recheck_days`, judged from CLI-authored telemetry only, bounded by the documented depth and at-once variables, with the resume, no-total-limit and server-flag residuals disclosed and the 6-launches-per-job limit dropped (no CLI control since 2.1.224); a missing, stale or failed record never refuses a run.
