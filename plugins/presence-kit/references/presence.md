# Presence model

How Claude behaves when the user is present, away (afk) or has handed work
over (autonomous). This file is the single source of the model; the skills
`presence-kit:afk`, `presence-kit:autonomous` and `presence-kit:recap` and the
hooks defer to it. Every behavior below has an id and can be switched off; the
catalog, defaults and file format are in `configuring.md` (same directory).

## Before applying anything

Run this first, in the same turn. It prints the resolved configuration as JSON
(`behaviors`: id to bool; `values`: `afk_quiet_minutes`, `departure_words`,
`waiter_model`, `progress_minutes`, `watch_after_minutes`):

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" config

A behavior id that is `false` does not apply: skip the rule that names it. If
the command prints an `{"error": ...}` object, tell the user its message (it
names the file and key) and apply no presence rule this turn.

Hook lines read `presence-kit [<ids>] prompt <prompt id>: ...`. They come from
this plugin's two hooks (`hooks/hooks.json`): the UserPromptSubmit prompt hook
(`scripts/presence.py prompt`) and the Stop hook (`scripts/presence.py stop`),
and each line names the behavior ids that produced it. The prompt hook speaks
before you answer: while the user is afk it says so once per afk episode (the
away line, said again after a context compaction, which drops it from the
context) and, on every prompt of the user's own, gives the instruction to give the
return recap when `recap-on-return` is on. It says neither on an
automatic prompt (an envelope or a cron firing, section Presence). When
the prompt contains a word from `departure_words` it sets the user afk itself
and tells you so (section Presence). The Stop hook detects a return at the end
of the turn and records it (section Return).

## Presence

The user is `present` or `afk`.

- Afk starts when the user declares it or when no message of the user's own
  arrives for `afk_quiet_minutes` (`afk-on-quiet`, decided by the Stop hook).
  The user declares it (`afk-on-departure`) by typing `/presence-kit:afk`, by a
  word from `departure_words`, or by leaving in words you recognize as leaving
  (the backup when no listed word was used; you invoke `presence-kit:afk`).
- A listed word is acted on by the prompt hook, before you answer: it matches
  the word on its own in any case, so "AFK." and "(afk)" count while
  `afk-on-departure`, `presence-kit:afk`, `afk_since` and a backticked `afk` do
  not. It writes the afk state itself and tells you in a line that begins "set
  the user afk". It does no inference beyond the word, so a prompt that quotes
  or discusses the word, or an automatic prompt, can set afk by mistake. That
  line gives you the correction: when the user is not leaving or the prompt is
  not the user's own message, run `state set present` (section Commands) and
  tell the user once in one line. The hook skips a prompt that starts with
  `/presence-kit:`, `<task-notification>` or `<agent-message from=` (a subagent
  report or another session's message). It also skips a prompt that exactly
  equals (after trimming) the prompt of a live session cron or `/loop`, which
  the Stop hook records after each turn. Two gaps remain: a cron created and
  fired before any Stop is not recorded yet, and a user typing a cron's exact
  prompt is treated as automatic. Any other automatic prompt carries no marker
  the hook can read, so your correction is the safeguard there. A skip says
  nothing about who sent the prompt. The prompts the hook skips also get no
  away line and no return-recap line while the user is afk: a wake is not the
  user, so it carries no reminder. The away line is said once per afk episode
  on the first prompt of the user's own; the hook records that in the state's
  `notices` list (`away|<afk_since>|<time>`) and says it again only after a
  context compaction (a `compact_boundary` record in the transcript after that
  time). The configuration is not in that line: run `presence.py config` (section
  Before applying anything) before you apply a rule.
- Already afk, a prompt with a listed word keeps the user afk: the line begins
  "kept the user afk" and no return recap is asked for that prompt. If that is
  wrong, because the user is back and only mentions the word, undo it the same
  way (and, with `recap-on-return` on, the line also tells you to give the
  return recap).
- A scheduled task, agent report, task notification or another session's
  message is not the user. You classify who sent a prompt; a hook line only
  suggests it.
- "go" authorizes work. It never means afk.
- The stored state is in `state get` (section Commands): `presence`,
  `afk_since`, `goal`, `goal_since`.

## Authority

Authority is `normal` or `autonomous`.

- Autonomous starts only on an explicit hand-over (`autonomous-on-handover`) or
  when the user types `/presence-kit:autonomous`. `/loop`, routines, `claude -p`
  and `--bg` never start it.
- It records the granted goal in state (`goal`, `goal_since`) and implies afk.
- It ends on the user's revocation (`state set normal`) or when the goal is
  done (`presence-kit:recap autonomous-done <prompt id>`).
- It grants only the stated goal. Autonomous mode changes no permission prompt,
  auto-mode decision, publication gate or prohibition.

## Return

A message of the user's own while afk always makes them present; the Stop hook
records it. With `recap-on-return` on, give the return recap before you answer
when you can tell the message is the user's own: invoke
`presence-kit:recap return <prompt id>` with the id a hook nudge gave. When the
Stop hook finds no return recap for the prompt, its nudge asks for one. With no
id in view, invoke `presence-kit:recap` bare; it reads state. With `recap-on-return` off, the user is present again and
no return recap runs. The prompt that set (or kept) the user afk is not a return.

## Ending recap

With `ending-recap` on, an away turn that ends with nothing owned running gets
one ending recap per afk episode: the Stop hook nudges for
`presence-kit:recap afk-ending <prompt id>` (in autonomy, `autonomous-done` when
the goal is done, else keep working). `recap-done` records the recap in the
state's `notices` list for that episode and that granted goal
(`ending-recap|<afk_since>|<goal_since or ->|<time>`), so a later quiet Stop in
the same episode nudges no more. The nudge returns when tool work happened after
the recap (a job finished and was handled, new work started), when a new afk
episode begins, and when a new autonomous goal is granted. A recap-done call is
not tool work.

## Modes

| Mode | You decide | You ask the user |
|---|---|---|
| Any | implementation, routing, verification, in-scope fixes, git the user has authorized, cheap-to-reverse calls | -- |
| Present | as above | decisions the user has claimed, product purpose, costly ambiguity about intent |
| Afk | as present | the same questions, parked for the recap; independent work continues |
| Autonomous | everything not gated; each load-bearing call gets a line in the recap | nothing beyond what a gate requires |
| User only | never: permissions, auto mode, publication gates and standing prohibitions hold in every mode | -- |

Where `awesome-kit:orchestrate` is installed, its consultation policy owns
seat routing for a load-bearing call; this plugin does not change that policy.

## Waiting rule

`waiting-guard`, all modes. Agent and workflow work keeps the terminal on
"working" by itself and gets no waiter. Non-agent work (a shell job, a Codex CLI
job, an external job) needs a waiter agent (section Waiter agent) so the
terminal shows work.

`liveness-watch`, all modes. A Monitor on the shipped watcher (section Watcher)
reports a stall after quiet minutes (and progress every `progress_minutes`,
which is off by default), which the job's own completion notice never does,
because a stuck job never completes. Arm one for a job you expect to run longer
than `watch_after_minutes` (20 minutes by default) that no waiter covers: arm it
with `watch.py --task <id>` right after the job starts. A job a waiter covers
needs none, because the waiter's wait.py reports the stall. A Monitor lives for
its `timeout_ms` (30 minutes unless you set more); when it ends while the job
still runs, arm another. The Stop hook finds the Monitor you armed for a job in
the main transcript (a Monitor call whose command names the job's id, joined to
its "Monitor started (task <id>" result) and checks the payload for it still
running. A running owned job at least `watch_after_minutes` old with none gets
one nudge: `No liveness watch on: <job>`; a job whose Monitor is no longer
running gets `Liveness watch ended while the job still runs, re-arm it on:
<job>`. A job's age runs from the transcript record that announced it (a
background Bash result, a Monitor start, an Agent launch result; for an agent
resumed with SendMessage, from that call). A job whose start the transcript does
not show (no record, no timestamp, or a compacted transcript) counts as old, so
it is nudged. `watch_after_minutes` 0 nudges at once. A Monitor, an Artifact
monitor, a finished task, a running waiter agent and a subagent's own job need
no watch. Like the waiting guard it speaks once per prompt. With `liveness-watch`
off, none of this applies.

Never hold the main session with a foreground wait. The Stop hook checks for a
waiter and nudges once per prompt. A finished task (status
`completed`, `failed` or `killed`), an Artifact live-updates monitor
(description starts with `live updates for artifact `) and a shell job or
monitor that a subagent started are not owned work: they need no waiter and no
watch. A subagent's jobs are its own, and it reports to you when it ends. The
hook tells them apart by the transcripts: a job announced in
`<session transcript without .jsonl>/subagents/agent-*.jsonl` and not in the
main transcript is the subagent's; a job it cannot place counts as yours. A job
the waiter launched is announced only in the waiter's transcript, so it is the
waiter's own.

A waiter you launched for a job you started covers that job while it runs (the
fallback in section Waiter agent): the hook reads the main transcript for an
Agent call with `subagent_type: presence-kit:waiter` whose prompt names the job's
id or its `tasks/<id>.output` path, joined to the launch result's `agentId`,
and the payload for that agent still running. A running waiter agent (its task's
`agent_type` is `presence-kit:waiter` or `waiter`, or its description starts
with `waiter:`) needs no watch itself. A waiter that ended while the job still
runs leaves the job uncovered: C fires for it even when an unrelated agent runs
(that agent keeps the terminal working, but nothing waits on this job), and M
follows once the job is old. The nudge lists the tasks it counted. With
`waiting-guard` off, none of this applies.

A subagent that waits on a background job it started (not an agent) does not end
its turn while the job runs, because an idle subagent leaves the terminal
showing idle. It runs wait.py itself in foreground Bash calls, the loop
`agents/waiter.md` runs: `"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}"
"${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/wait.py" --output
<task output file> --stall-minutes <n>` with `timeout: 170000` (slices of 150 s,
never above 160), rerun on exit 4, stop on exit 0, 1, 2 or 3. It
does not spawn a waiter. (Hypothesis, untested: whether a waiter spawned by a
subagent keeps the main terminal working.)

The guard speaks once per prompt: it never repeats the waiting nudge within a
prompt, and a condition it left unsaid goes to the unresolved list for the
recap. Only the recap nudges (return, ending) repeat, up to three times per
prompt. When the user asks you to stop hearing about waiting, set
`waiting-guard` to `false` in the user config layer (`configuring.md`) and tell
the user once in one line; the guard then says nothing.

## While away

While the state is afk, and each only when its id is on:

- `terse-while-away`: keep interim messages short; nobody reads them until the
  user returns. Off: write messages at normal length.
- `park-questions`: do not ask mid-run; park the question for the recap and
  continue independent work. Off: ask as you would when present.
- `notify-for-input`: send a push notification only when the user's input is
  needed. Off: this rule does not apply.

Nothing is dropped: the recap carries every result, decision, question and
running job.

## Waiter agent

Covers one non-agent job, and starts it. Start a background Agent with
`subagent_type: presence-kit:waiter` (a Bash-only agent: it carries no other
tools, so it costs far less context than a general-purpose agent), the
`waiter_model` value from `presence.py config` as its `model`, and a
`description` that starts with `waiter:` (the Stop hook recognizes a waiter task
by its agent type, and by that prefix when the type is not shown). Its brief
names the job's exact command and working directory and the `--stall-minutes`
you derive from it (the longest quiet period the job can show). The agent starts
the job with Bash `run_in_background: true`, takes the output path from the tool
result ("Output is being written to: <path>"), runs this until exit 0, 1, 2 or
3, and reports one line: the job's exit code, the output path and, only on
failure, a tail of at most 20 lines. Its completion wakes the main session; you
never see the job's task id, so you arm no Monitor and the hook asks for none:

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/wait.py" --output <output path> --stall-minutes <n> --path <the job's directory>

A background job a subagent starts survives that subagent ending its turn
(measured on 2026-11-18). The job is not in the main session's task list, so
TaskStop on its id finds nothing: to stop a waiter-launched job, TaskStop the
waiter. (Hypothesis, untested: stopping the waiter stops the job it started.)
Agent and workflow jobs get no waiter: they keep the terminal working by
themselves.

Fallback, when you start the job yourself and the waiter waits on it: use it for
a job that is already running, a job that needs state only the main session has
(an environment, a login), a job that is not one command, or when the waiter's
own launch is denied (a background subagent cannot ask the user for
permission). The brief then names the job's output file (its task output path)
instead of a command, and the agent runs the same wait.py command on that file
and reports the same line, which costs two wakes in all. The hook then treats
the job as covered while that waiter runs (section Waiting rule).

Use `--file <path> --pattern <regex>` instead of `--output` to wait for a
pattern in a file. `--slice-seconds <s>` defaults to 150; leave it alone. The
waiter runs wait.py in a foreground Bash call with `timeout: 170000` (the
default Bash timeout is 120000 ms, below a 150 s slice). The harness moves any
foreground command that runs past about 180 s to the background ("Command did not
complete within its 180s timeout and was moved to the background"), whatever
`timeout` the call sets (a `timeout: 600000` call was moved four times). The
waiter then ends its turn waiting on its own background job and the terminal
shows idle, which defeats the waiter. A 150 s slice returns before that window, so
the waiter is between calls for about a second and stays a running agent
(terminal shows working) from slice to slice. Never pass a `--slice-seconds`
above 160, never run wait.py in the background, and keep any other poll the
waiter writes (for example a `sleep 5` loop checking the done condition) under
160 s per call. The waiter runs it again on exit 4. The brief names the job's exact output file (its task output path);
the waiter never picks a file by recency, because it can then watch another job.

A quiet file alone is not a stall when `--path <dir>` (repeatable) or `--pid
<pid>` (repeatable) is given: wait.py then samples processes as the watcher does
(section Watcher, the same `scripts/procwatch.py`), and a process whose working
directory is inside a `--path` directory, or a `--pid` process or its descendant,
that used CPU keeps the job alive. The first poll of a slice only baselines, so it
cannot report a stall. Slices are separate processes, so the time of the last
activity seen is kept in one small file in the temp directory
(`presence-kit-wait-<hash of the output path>.json`), removed on exit 0 and 3.
Without those options wait.py samples nothing and writes nothing.

| Exit | Meaning |
|---|---|
| 0 | done: prints the `[exited with code N]` trailer or the match |
| 3 | stalled: no size or mtime change for n minutes, and no process of the job using CPU when `--path` or `--pid` is given |
| 4 | slice over, job still live: run the command again |
| 2 | bad arguments, or no file within the stall window |
| 1 | process sampling failed (only with `--path` or `--pid`): the reason is on stderr |

## Watcher

`scripts/watch.py` is the Monitor's command for any owned job. Arm a Monitor
on it instead of writing a watcher script of your own: its cadence is fixed, so
the user is not flooded with progress lines (PROGRESS is off unless you set
`progress_minutes`). It prints one line per event and runs until its done condition holds or it is killed. For a background task, pass
its id:

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/watch.py" --task <task id> [--path <extra dir> ...] [--stall-minutes <n>]

`--task` derives the paths from the id and `CLAUDE_CODE_SESSION_ID`: the task's
output file `<tmp>/claude-<uid>/<project slug>/<session id>/tasks/<id>.output`
(found by pattern; `<tmp>` is `/tmp`). On Windows it is
`<tmp>/claude/<hash>/<session id>/tasks/<id>.output` (no uid suffix; `<tmp>` is the
temp directory, `%TEMP%`; the hash is found by pattern too). A shell task's output file is watched and
its `[exited with code N]` trailer is the done condition. An agent task's output
file is a symlink to the agent's transcript (on Windows it is an empty regular file, and the
watcher finds the transcript at `<config dir>/projects/*/<session id>/subagents/agent-<id>.jsonl`
instead; no transcript is an exit 2); the watcher also watches the working
directory that transcript records (the agent's worktree), and ends with `DONE:
agent handed back its report` once the transcript's last record answers the
agent's SubagentHandback call. An agent resumed later by SendMessage needs a new
watcher. File changes under `.git` and tool caches (`.pytest_cache`,
`__pycache__`, `.mypy_cache`, `.ruff_cache`) are not activity. A missing session
id or a missing output file exits 2 with the reason. Process sampling on Windows uses the
Win32 API (procwatch.py); a Windows link that cannot be resolved is skipped when the watcher
looks for the newest change. Each poll walks every watched directory, so a very large worktree
makes a poll slow (tens of seconds on a repository the size of Spirit Crossing); use a
`--poll-seconds` well above that. For
anything else, name the paths yourself:

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/watch.py" --path <file-or-dir> [--path ...] [--done-output <task output file> | --done-file <path> --done-pattern <regex>]

`--done-file` requires `--done-pattern`, and `--done-pattern` goes with
`--done-file` only (otherwise exit 2: `--done-file requires --done-pattern`);
`--done-output` and `--done-file` exclude each other.

Every `--poll-seconds` (default 300) it takes the newest modification time
across the `--path` values: a file's own mtime, or the newest mtime of any file
under a directory (`.git` directories are skipped). A missing path counts for
nothing. The done file is watched too.

A running process is activity too, because a long command (a pre-push hook, a
build, a test run) changes no file. When a `--path` is a directory, or `--pid
<pid>` is given (repeatable), each poll counts a process that used CPU since the
previous poll: its working directory is inside a watched directory, or it is a
`--pid` process or one of its descendants. It must have used at least 1% of one
core over the interval, so an idle process does not count. A process first seen
at a poll is only a baseline, so the first poll of a directory watch cannot
report a stall. The watcher's own process and its ancestors (the Monitor shell,
the session) never count. A `--task` shell job whose command runs in some other
directory is covered by naming that directory with `--path` or the job's pid
with `--pid`. Sampling (`scripts/procwatch.py`, shared with wait.py) uses `ps`
plus `/proc/<pid>/cwd` (Linux) or `lsof` (macOS), and `lsof` runs only for the processes that used CPU. On Windows
(native Python, including under Git Bash, whose `ps` has no `-A`) it calls the Win32
API instead: a Toolhelp snapshot, `GetProcessTimes` and each process's own
working directory; a process whose directory cannot be read (another user's) is
skipped. A sampling error
prints `process sampling failed: <reason>` to stderr and exits 1: it is never
read as no activity. `--stall-minutes` (default 20) and
`--progress-minutes` (default: the `progress_minutes` value, 0) change the
cadence for one run. It never writes a file.

| Line | Meaning |
|---|---|
| `PROGRESS <elapsed>m: last change <age>m ago in <path>` | every `--progress-minutes`, changed or not; off by default (`progress_minutes` 0, and `--progress-minutes 0`: no PROGRESS lines); `no watched path exists yet` replaces the tail while no watched path exists; `last activity <age>m ago: process <pid> (<command>) using CPU` replaces the tail when the latest sign of work is a process, not a file |
| `STALL: no change in watched paths for <N>m` | no file change and no process using CPU for at least `--stall-minutes`; printed once per quiet episode, re-armed by any newer mtime or process activity; the watcher keeps running |
| `DONE: <line>` | `--done-output`: the last non-empty line is `[exited with code N]`; `--done-file`: the pattern matches a line; `--task` on an agent: the agent handed back its report; the watcher then exits 0 |

A `STALL` line reports a fact and prescribes nothing: what to do about the job
is your decision. While the user is afk and `notify-for-input` is on, notify them of
every `STALL` you receive with the PushNotification tool, one line naming the job and the quiet minutes,
because a stalled job waits for them and they are not watching the terminal (the
watcher cannot push; you do). An invalid configuration or bad arguments print to
stderr and exit 2; a process sampling failure exits 1.

## Commands

All take `--session ID`; the default is `CLAUDE_CODE_SESSION_ID`. Each prints one
JSON object. A `{"skipped": ...}` reply means stop: change nothing and tell the
user once. `--by user` only when the user typed `/presence-kit:afk` or
`/presence-kit:autonomous` themselves; otherwise `--by model`, which is skipped
when the matching id is off. `state set normal` and `state set present` take no
`--by`.

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" config
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state get
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set afk --by user|model
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set autonomous --by user|model --goal "<granted goal>"
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set normal
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" state set present
    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${PRESENCE_KIT_ROOT:?presence-kit is not provisioned}/scripts/presence.py" recap-done return|afk-ending|autonomous-done <prompt id>

`state set afk` is governed by `afk-on-departure`, `state set autonomous` by
`autonomous-on-handover`. `state set normal` revokes authority and leaves
presence unchanged. `state set present` undoes afk: presence is present, the
pending return and the hook's prompt record are cleared, `afk_since` is kept,
and no behavior id gates it; it is the correction for a wrong departure match
and leaves authority alone. `recap-done` is what the Stop hook checks after a recap:
recap-shaped prose without it satisfies nothing.
