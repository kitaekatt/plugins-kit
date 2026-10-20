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
Use `~/.claude/task.local.yaml` for user/machine choices already in that domain: `dashboard.max_parallel_projects` (default 2), `default_timeout_s` (7200), Claude/Codex model entry defaults, effort defaults, Codex network (false), and endpoint-helper declarations (empty). Per-queue model, effort, network, and timeout override these defaults, are disclosed in the UI, and are stored only in runtime state.
These seams pass the plugin-opinion razor because concurrency, wall time,
subscription choice, network access, and external-provider use each carry a
real cost. Loopback binding, one active item per project, no automatic commits,
and explicit Retry remain hard requirements rather than preferences.
## Concurrency and crash recovery
- Queue order is FIFO within a project; `Run next` changes only that project.
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
  and `codex exec`. Path-inexpressible delete rules deny the command family.
- Refuse launch if the installed Claude CLI cannot represent the floor; do not
  silently drop a deny rule.
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
- Enable that instruction only after a live headless characterization test for
  the exact CLI proves the tool is callable and permissions are inherited.
- If the test fails, the orchestrator works alone and the UI discloses that
  native sub-agents are unavailable; it never substitutes another top-level model.
- Maximum native depth is 1, maximum concurrent children is 3, and maximum
  child launches per job is 6. Children may not launch children or jobs.
- `TASK_KIT_AGENT_DEPTH` is set to 0 for the orchestrator and 1 for children;
  nested `task-kit`, `llm-kit jobs run`, `codex exec`, or `claude -p` refuses at
  depth 1 and is included in each worker's deny floor.
- Configured OpenRouter/local endpoints are available only through a bounded
  task-kit sub-work helper that calls llm-kit completion without tools.
- Endpoint helpers receive an explicit excerpt/prompt, return text to the
  orchestrator, and never own acceptance or write the checkout.
- No endpoint helper is exposed unless configured; use is logged in run history.
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
None open. New task defaults to `dev/tasks`; endpoint-helper consent is per project.
## Premise outcomes
- CONFIRMED WITH CONSTRAINTS - one item can be one flat llm-kit Job with a shared
  directory and one-prompt/one-contract ledger. The agentic Claude deny floor must
  live in `Job.options.disallowed_tools`, not JobFile/run-level floor; v1 uses
  run-wide timeout, rejected blocks, existing events, and one cancelled-state change.
- UNTESTED - a headless `claude -p` job can call Agent and `codex exec` can call
  its native spawn-agent mechanism end to end. Source advertises/recognizes the
  tools, but no cited live job proves invocation and permission inheritance.
- CONFIRMED - published task-kit can host the dashboard behind an optional
  llm-kit REFUSE edge: bootstrap soft-links optional shared libraries, supports
  manual discovery, and existing task modules do not import llm-kit.
- CONFIRMED - Afterplay's launcher is macOS/Linux only. Its source uses `lsof`,
  `ps`, POSIX process groups, no Windows browser command, and Windows bootstrap
  skip; this design replaces those mechanics rather than copying them.

## Disagreements with review

- Finding 7's outcome is accepted, but its evidence calls `list_events` read-only. It is `JobStore.list_events`, and even `create=False` constructs `JobStore` then always migrates (`$DEVROOT/plugins-kit3/plugins/llm-kit/lib/llm_kit/jobs/store.py:740,759-763,817-829,2537`; caller `$DEVROOT/plugins-kit3/plugins/llm-kit/lib/llm_kit/cli/jobs.py:361-366`). Only `LedgerReader.snapshot` is explicitly read-only (`$DEVROOT/plugins-kit3/plugins/llm-kit/lib/llm_kit/jobs/store.py:2850,2910`). V1 still drops the new tail flag; live runs use `LedgerReader.snapshot` and events are read only after runner exit (round 2, N1).

## Revision log

- 1 fixed - task-scoped, one-directional holds; harness always proceeds.
- 2 fixed - Codex network defaults off and runs require a live deny-policy gate.
- 3 fixed - prevention floors replace attribution; ancestry and status delta are observational.
- 4 fixed - Claude floor is only `Job.options.disallowed_tools`; argv tests required.
- 5 fixed - run-wide timeout retained; acceptance starvation is a typed block.
- 6 fixed - blocked contracts reject; v1 has no interrupt/waiting leg.
- 7 fixed - new tail CLI removed; evidence detail is disagreed above.
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
