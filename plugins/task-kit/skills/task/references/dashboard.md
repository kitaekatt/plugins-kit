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

- A run cannot start other workers: the `Agent` (sub-agent), `Workflow`, `EnterWorktree`, `RemoteTrigger`,
  `CronCreate` and `ScheduleWakeup` tools are denied, workflows are switched off (`CLAUDE_CODE_DISABLE_WORKFLOWS=1`),
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

## Recovery and settings

Runs survive the dashboard server: a restarted server adopts the live runner from its run directory and settles a finished one from its receipt, never starting a second runner. A runner that vanished is crash inspection: its leftover processes are stopped, its ledger is closed as cancelled, and the task is blocked with the run shown `Crashed`; nothing is retried or reported complete. A receipt that cannot be read blocks and never counts as success. The ordered recovery table is documented at the top of scripts/task_system/dashboard/scheduler.py.

Machine settings live under `dashboard:` in `~/.claude/task.local.yaml`: `max_parallel_projects` (checkouts running at
once, default 2), `default_timeout_s` (default 7200), `default_worker` (claude or codex, default claude),
`claude_entry` (default opus), `codex_entry` (default sol), `effort` (default unset), `codex_network` (default false),
`endpoint_helpers` (default none; see Endpoint helpers).

## Related

`review` still writes its one-off HTML snapshot; the dashboard is a separate, live view and does not replace it.
