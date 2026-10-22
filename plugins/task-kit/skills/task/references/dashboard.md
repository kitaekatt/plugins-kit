# The task dashboard

Load this when the `dashboard` capability of the task skill is routed and the question goes beyond launching it.

## What it shows

The dashboard shows `dev/tasks` tasks only, as one flat list across projects (`tmp/` tasks are not shown). A project is a git repository: the basename of the `origin` remote URL, or the directory name when there is none, so every checkout of one repository is one project. A task is displayed as `<repo>:<stub>`; its id everywhere else (task.py, task_list refs, the API's task id) stays the path `dev/tasks/<stub>`, and the checkout it was read from is its working root, the directory a run executes in.

`task validate` warns about an uncommitted `dev/tasks` folder. The dashboard never gates on that warning (queueing, acceptance after a run, Re-check), because a run never commits and the dashboard's own block and Re-check writes leave the folder uncommitted; every other error and warning still gates. The task details show it as information.

## What it writes

A refresh never edits a task folder. The dashboard writes one only for its own actions -- recording a block (task status blocked, the item blocked-user with a note, one log.md line) when a run or its translation fails, Re-check, and the authoring actions New task, Add item and Defer/Undefer -- each under the folder's expected revision, one dated `dashboard:` log.md line per write, and each refused (409 `held`, `run-active`) while a `/task work` session holds the task or a dashboard run works it. A write compares each file just before replacing it, so a harness edit that landed first is never overwritten, but the files are replaced one at a time (a partial write is reported by file) and a hold-less harness write landing inside one compare-and-rename can be lost. A dashboard write leaves the folder uncommitted, which `/task work` reports until it is committed, as after a hand edit. The task.py verbs and the harness workflow are unchanged whether or not a dashboard is running.

## Actions

Queue, Queue all, Retry, Re-check, Cancel run, Remove from queue, New task, Add item (and add-and-queue), Defer/Undefer and the run options are live. Queue, Queue all, Retry and add-and-queue start unattended Claude runs, one item each, in the project checkout and need llm-kit; without it they refuse with the reason shown, and everything else keeps working. Run options (worker, effort, time limit) belong to one queue entry and are saved while it waits. Pause worker is shown disabled. The dashboard's Resolve in Claude Code only prints commands to copy.

Cancel run (POST /api/v1/runs/<id>/cancel) is process control on a run id and carries no task revision: a queued entry is normally taken out with Remove from queue (DELETE /api/v1/queue/<id>, ending `removed`, which is not retryable); a running one has its whole process tree stopped and its llm-kit ledger closed as cancelled (a run that hits its time limit closes its ledger the same way, reason `run-wide timeout`). A cancelled run is retryable like a timed-out one and does not block the task; look at the shared tree before retrying, because partial work stays there. A stop that leaves a process running, or a ledger that cannot be closed, blocks the task for inspection instead.

An item is queueable only when the task validates clean and is active, the item is available, and plan.md has exactly one non-empty section headed with the item id (`### <id> -- <title>`). Otherwise the task is blocked and the note says what to fix; nothing is guessed. An item may carry `accept:` in its task_items entry, a list of argv lists the dashboard runs from the project root after the worker finishes (never through a shell); without it only structural acceptance applies. A run never commits, branches or pushes; review and commit stay with the user.

A blocked run is resolved here: `/task work <ref>`, fix or finish the item, then Re-check in the dashboard; the next Queue is a fresh run. Codex workers are refused until a live safety test has passed.

A run that timed out or ended without a verdict is shown with Retry, which is also a fresh run. Only one run per checkout is live at a time.

## Start order and waiting

- Several checkouts run at once, up to `max_parallel_projects`; one checkout never runs two. Queued entries start in
  a fair order: the checkout that started a run least recently goes first, and entries of one checkout keep their
  queue order. "Queued #n" in the page is that order.
- The scheduler starts one entry per pass and then takes another pass, so Cancel and Remove never wait for a whole
  queue to start.
- A queued entry that cannot start says why next to its place: `config` (the `dashboard:` section is unusable),
  `llm-kit-unavailable`, `held` (a Claude Code session works the task), `checkout-busy` (another run works that
  checkout), `queued-behind` (an earlier entry of the checkout starts first), `no-slot` (all
  `max_parallel_projects` checkouts are running) or `cli-check` (the installed Claude Code is being checked once).
- A task is never blocked while a Claude Code session holds it or a dashboard run works its folder: Queue answers
  422 `translation-failed` with `blocked: false` and the entry stays where it was.

## What a run may not do

- A run cannot start other workers: the `Workflow`, `EnterWorktree`, `RemoteTrigger`, `CronCreate` and
  `ScheduleWakeup` tools are denied, and so is `Agent` (the sub-agent tool) unless native sub-agents are on (see Native
  sub-agents). Workflows are switched off in every run (`CLAUDE_CODE_DISABLE_WORKFLOWS=1`),
  and the Bash floor denies git history changes, deletes, publication and the nested drivers (`claude -p`, `codex
  exec`, `task-kit`, `llm-kit jobs`, `llm-kit pipeline`, `llm-kit completion`). These are tripwires against a careless
  worker, not confinement.
- The first Claude run on each Claude Code binary waits for a one-time check (about two minutes at most) that the
  binary honors those denials. A binary seen to ignore a denial refuses Claude runs (Queue answers 422 `bad-options`,
  reason `cli-deny-ignored`); a check that could not conclude lets runs start with the denials and the page says so.
- `task-kit` refuses inside a dashboard run (exit 6): it may not start, stop or restart its dashboard, start another
  run, or run another worker. The refusal checks the inherited `TASK_KIT_AGENT_DEPTH` marker and whether the process
  descends from a live runner of the dashboard it targets; a dashboard on another `--data-dir` is not refused, so
  task-kit's own tests can run inside a worker.

## Native sub-agents

A Claude run may launch sub-agents of its own only when a current characterization says the installed Claude Code keeps
to the limits the dashboard relies on. Without one the sub-agent tool is denied, the run still starts, and the run
controls say why and show the command to copy.

- `task-kit characterize` runs three short real Claude sessions (`--entry` picks the model entry, default `dashboard.claude_entry`; a small one costs little) in a scratch directory under the dashboard root, and checks from the CLI's
  own events and from files: a run can launch a sub-agent; the denied commands stay denied for the run and for its
  sub-agent; a sub-agent cannot launch another (depth 1); and a second launch while one runs is refused (at most
  `max_concurrent` at once). What the model says in its answer is never evidence, and a check the events do not settle is
  `inconclusive`, not a pass. It prints every check, writes the record whatever the verdict and exits 0; it exits 1 when it
  cannot run (no Claude Code on PATH, llm-kit missing) and 2 for `--worker codex`. Like every `task-kit` command it is
  refused inside a dashboard run.
- The record is tied to the Claude Code binary (its SHA-256 digest and version) and expires after `recheck_days`. A Claude
  Code update, an edited record, a different version of the check or the passing of the expiry turns sub-agents off until
  the command is run again; nothing re-runs it for you. A record that exists but cannot be read is reported as corrupt.
- When on, a run gets two of Claude Code's documented per-session limits in its environment
  (`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1` and `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`), keeps every other denial, and its
  prompt says what it may do. The page says what Claude Code documents as outside those limits: a resumed sub-agent can
  exceed the at-once limit, there is no total limit, and Claude Code can lift the at-once limit from its servers.
  Workflows and ultracode stay off.
- A run's recorded decision (on or off, why, the limits) is shown in its latest-run card.

```yaml
dashboard:
  subagents:
    enabled: true        # false keeps sub-agents off whatever the record says
    max_concurrent: 3    # sub-agents running at once in one run (at least 1); nesting depth is fixed at 1
    recheck_days: 7      # how long a passing characterization keeps them on (at least 1)
```

## Endpoint helpers

A run can ask a text-only model (a helper: no tools, no file access, never a worker) a question when you configure one
for the repository. Nothing is offered by default. In `~/.claude/task.local.yaml`:

```yaml
dashboard:
  endpoint_helpers:
    local-qwen:                      # name: lowercase letters, digits and hyphens; not claude or codex
      models: [qwen-local]           # registry entries, tried in order; each must be a transport (text-only) entry
      repositories:                  # consent: the repositories a run may use it in
        - https://github.com/org/repo      # any spelling of the origin URL
        - /path/to/checkout-with-no-origin # an absolute path, for a checkout that has no origin
      purpose: "summarize long logs"       # optional, one line, 200 characters at most
      max_calls: 20                  # per run, per helper (default 20)
      timeout_s: 300                 # per call (default 300)
      max_input_chars: 100000        # input plus system text together, at most 1000000 (default 100000)
```

- Consent is by repository identity (the normalized `origin` URL, or the real path without an origin), never by the
  displayed project name. A helper is offered only to runs in a listed repository, and the run's prompt then names it
  and the command that asks it; the page shows "Endpoint helpers for this repository" in the run controls.
- A helper whose models include an agent (a harness entry) is refused: Queue answers 422 `bad-options` with reason
  `endpoint-helper`. A model the registry does not know fails at the call (`unroutable`, with every entry's reason).
- The dashboard server, not the run, decides what is offered, how many calls remain and whether the run is still live.
  The run holds one token, issued when it starts and revoked when it ends; a call after that is refused (410), a spent
  budget is 429, an unlisted helper 404, an oversize or empty excerpt 422. The command exits 0 when answered, 2 when
  refused, 3 when no usable endpoint remains and 1 when the call failed or the dashboard cannot be reached.
- Every call and refusal is recorded; the run's details list them (helper, entry, outcome, time, characters, tokens and
  cost when reported). The raw excerpts and answers stay in the run directory (`helpers/`) and are not served. A helper
  call is a paid call to a model you configured; send only what the question needs and never a credential.
- This stops a run from widening its own offers or budgets. It does not stop a process that calls the model library
  directly or reads an endpoint key from your data directory.

## The /task work handshake

`task work <ref>` is preceded by `task_hold.py <ref> --session "${CLAUDE_SESSION_ID}"`, which tells a running dashboard that this session is working that task folder, so the dashboard stops starting autonomous runs on it while the session lives. It is bounded by 2 seconds, never gates the work verb and always exits 0. With no dashboard running it prints nothing. Otherwise it prints at most one line on stderr and nothing on stdout. When a dashboard run is already live on the same task, that line names its run id and state, says the run keeps running and its edits may collide with the session's, and says to cancel it in the dashboard (Cancel run) or let it end by itself or at its time limit. The run is never touched by the handshake.

## Working-session registrations

`/task work` registers the session in git (`<task root>/.sessions/<host>.yaml`) and publishes it: it commits this machine's file on the task root's checked-out branch, fetches the upstream, fast-forwards or merges it into the checked-out HEAD and working tree (as a pull would; a merge that stops on conflicts is aborted), and pushes only when the net change from the upstream is session files. It never force-pushes, rebases, stashes or switches branches. `sessions: {record: false}` in `~/.claude/task.local.yaml` turns registration and publication off. The dashboard reads those files for every physical task root and shows each registration on the task row as a small inline marker, with the full list in the task details:

| Marker | Meaning | Effect |
|---|---|---|
| `Claude Code session working (<host>, since <time>)` | this machine's record, session verified running (process id plus exact creation stamp) | **holds the task**: no run starts or queue-starts on it, authoring is refused (409 `held`, naming the session), and a queued run's waiting reason names the session |
| `not running (<host>, <age>)` | this machine's record, its process is gone | shown only; blocks nothing and is never released because the process ended |
| `on <host>, <age>` (tinted; the state "unverified" is in its tooltip and aria-label) | another machine's record, which this machine cannot verify | shown only; blocks nothing |

We never prevent an action because of an unverifiable state, so only the first kind holds. Re-check reports the registrations that remain and never releases one; a registration ends at `/task hand-off`, or with `task sessions release <ref> --all-local`. The dashboard has no Release button.

**Dismiss** (task details, on an unverified record). The dismissal is an authoritative fact, not a local view setting. It is recorded in git, in this machine's own session file (`dismissed:` with the target's host, session id and `registered_at`, plus `dismissed_at`; the file's host says who), and committed and pushed like a registration (the same fetch, fast-forward or merge, and push), so no machine ever edits another machine's file. Every reader hides the dismissed record at once. The owning machine drops it at its next write even if its process looks alive; a session that is in fact still working registers again at its next `/task work` (a new `registered_at`) and shows again. This machine drops its dismissal entry once the target record is gone.

In the background (one pass per minute) the server reconciles this machine's session files -- records on closed, archived or missing tasks, records some machine dismissed, and dismissal entries whose target is gone -- and publishes this machine's pending session files in the same way, bounded, unless `sessions.record` is false, reporting each outcome in the server's own log (the data directory keeps only lock files, never session state). It never pushes a file that is not this machine's own.

## Launcher exit codes

The launcher (`task-kit`, or `/task dashboard`) exits 0 ok, 1 error, 3 `status`: not running, 4 server up but the browser could not be opened (the URL is still printed), 5 refused because jobs are running, 6 refused inside a dashboard run. `--no-open` (or `TASK_KIT_NO_OPEN=1`) skips the browser. A corrupt server record, or something else answering on the recorded port, stops the launcher with a message naming the file.

How the browser is opened depends on the platform, and the launcher prints what was confirmed, never that a window opened:

- macOS runs `open` and judges it by its exit status. A failure to start it, a nonzero exit (with its stderr) and ten seconds without an exit are three distinct failures (exit 4).
- Linux starts `xdg-open` in its own session with its output appended to `browser.log` in the dashboard runtime directory. A nonzero exit is a failure (exit 4) and shows the log's last line. An opener still running after ten seconds counts as accepted and is left running, because `xdg-open` can be the browser itself.
- Windows asks the shell to open the URL (`os.startfile`).

## Security

The dashboard's runtime directory is owner-only (0700) and `server.json`, which holds the token that authorizes queueing runs, is owner-only (0600) on macOS and Linux; an existing, more open directory is tightened when the server starts and the server log says so once. On Windows the profile's access control applies and nothing is changed. Every mutation route is listed in one table the server enforces, a request to any other mutation route answers 405, and a connection that sends nothing for 30 seconds is closed.

## Recovery and settings

Runs survive the dashboard server: a restarted server adopts the live runner from its run directory and settles a finished one from its receipt, never starting a second runner. A runner that vanished is crash inspection: its leftover processes are stopped, its ledger is closed as cancelled, and the task is blocked with the run shown `Crashed`; nothing is retried or reported complete. A receipt that cannot be read blocks and never counts as success. The ordered recovery table is documented at the top of scripts/task_system/dashboard/scheduler.py.

Machine settings live under `dashboard:` in `~/.claude/task.local.yaml`: `max_parallel_projects` (checkouts running at
once, default 2), `default_timeout_s` (default 7200), `default_worker` (claude or codex, default claude),
`claude_entry` (default opus), `codex_entry` (default sol), `effort` (default unset), `codex_network` (default false),
`endpoint_helpers` (default none; see Endpoint helpers), `subagents` (see Native sub-agents).

## Related

`review` still writes its one-off HTML snapshot; the dashboard is a separate, live view and does not replace it.
