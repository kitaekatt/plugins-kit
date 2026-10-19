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
live browser view over every project's task folders: each task's items as a
pipeline, what needs you (blocked tasks and items), what is running, and the
newest log entries. A project dropdown and a search box filter it.
`Resolve in Claude Code` shows the commands that open a task in Claude Code; it
does not run them.

Queue runs one item as one unattended Claude job and needs the llm-kit plugin;
without it the autonomous actions refuse with the reason and the rest keeps
working. Queue, Retry and Re-check are live; other controls are drawn but
disabled. The contract (eligibility, blocked-run resolution, holds, the
`dashboard:` settings in `~/.claude/task.local.yaml`) is owned by the dashboard
capability in `skills/task/SKILL.md`.

The dashboard runs beside the task system: the `task` verbs and the Claude Code
workflow behave the same whether or not it is running. A refresh never edits a
task folder; the dashboard writes one only to record a block or a Re-check, under
the folder's expected revision.

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
