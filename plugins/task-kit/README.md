# task-kit

File-backed task tracking for Claude Code: a task owns a folder, documents
reference it, and every verb names its task explicitly.

## What it does

The `task` skill drives one CLI over task folders (`tmp/<stub>` for
ephemeral work, `dev/tasks/<stub>` for version-controlled work): create,
list, show, validate, work, review, summarize, close, reopen, archive,
delete, and move (which rewrites every `task_list` reference to the moved
folder). The skill routes a natural-language request ("start a task for X",
"what tasks are open here?", "archive the closet task") to the right verb and
states what the agent, rather than the script, must do with the output.

The skill's entry point, verb contracts, and design documents are in
[skills/task/SKILL.md](skills/task/SKILL.md) and
[skills/task/design/task-system-design.md](skills/task/design/task-system-design.md).

## The dashboard

Type `task-kit` in any terminal (or `/task dashboard` in Claude Code) to open a
live browser view over every project's task folders: one list of open tasks,
one line each, with a marker for tasks that need you (blocked tasks and items)
and for tasks with a job in flight. Click a task to open its items as a
pipeline (one at a time). A sort control (priority, needs you first, in progress
first, latest activity), a project dropdown and a search box shape the list;
closed tasks and recent activity sit below, collapsed. Selecting an item (a stage bubble)
opens its detail under the stage map; everything stays inline and full width. A project is a git repository: every checkout of one
repository is one project, and a task is shown as `<repo>:<task>`. The dashboard
lists `dev/tasks` tasks only. `Resolve in Claude Code` shows the commands that
open a task in Claude Code; it does not run them.

Queue runs one item as one unattended Claude job and needs the llm-kit plugin;
without it the autonomous actions refuse with the reason and the rest keeps
working. Queue, Queue all, Retry, Re-check, Cancel run, Remove from queue, New task,
Add item, Defer/Undefer and the run options (worker, effort, time limit per queue entry)
are live; Pause worker is drawn but disabled. Cancel run stops the run's whole process tree and closes its ledger as
cancelled; the run is then retryable, and its partial work stays in the shared tree
for you to look at. A run is never retried by itself: if the dashboard server
restarts, the running work is adopted again from its run directory, and a runner
that vanished leaves the task blocked for inspection (the recovery table is at
the top of `skills/task/scripts/task_system/dashboard/scheduler.py`). The contract (eligibility, blocked-run resolution, holds, the
`dashboard:` settings in `~/.claude/task.local.yaml`) is owned by the dashboard
capability in `skills/task/SKILL.md`.

The dashboard runs beside the task system: the `task` verbs and the Claude Code
workflow behave the same whether or not it is running. A refresh never edits a
task folder; the dashboard writes one only to record a block, a Re-check, a New task,
an Add item or a Defer/Undefer, under the folder's expected revision and never while a
`/task work` session holds the task or a run works it.

```
task-kit               start (or reuse) the dashboard and open it in your browser
task-kit status        report the running server
task-kit stop          stop it
task-kit restart       stop it and start it again
task-kit --no-open     do not open the browser (also TASK_KIT_NO_OPEN=1)
```

One server runs per machine on `127.0.0.1`, on a port the operating system
chooses; the page is served with a per-run token and a restrictive CSP. If the
installed code changed since the server started, the next `task-kit` restarts
it. Projects are the directories under `project_directories` in
`~/.claude/task.local.yaml` (or `DEVROOT`), as for `task list --scope all`.

bootstrap puts the launcher on your PATH (`~/.claude/plugins/data/plugins-kit/task-kit/bin`;
on Windows the User PATH), so a new terminal finds `task-kit` after the first
bootstrap pass that provisions this version.

## Install

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install task-kit@plugins-kit
```

task-kit declares dependencies on `bootstrap` (venv provisioning),
`skills-kit` (the `skills_kit_lib` schema engine the task CLI imports), and
`awesome-kit` (the `work` verb emits `Skill(awesome-kit:orchestrate)` as its
always-required baseline skill). Installing task-kit installs all three.

## Try this first

Ask for a task: "start a task for tidying the closet", or invoke the skill
as `task-kit:task`.

## When not to use it

Claude Code's native task tracking (TaskCreate / TodoWrite) is a different
system; task-kit is for work that outlives a session.
