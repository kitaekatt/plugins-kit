# Query operations

Load the selected section for task discovery, field inspection, item menus, or
working-session registrations.

## list

Operation signature: `task.py list [--scope all|user|project|skill|file]
[--target X] [--status S] [--priority P] [--format text|json|yaml]
[--root PATH]`.

Default output has three sections in order: Open tasks, Deferred tasks, and
Closed tasks. Open means active or blocked. Deferred means purposefully on
hold and intended to resume. Each line is
`id  status  priority  last_update  title`; absent fields are `-`, and
last_update is the latest ISO date in dated log entries. Explicit `--status`
prints only matching tasks, including archived and other classifications.
List deduplicates by canonical path, validates each task, and exits 0 when
empty. Notes and summary warnings use stderr.

Text format preserves the line output. JSON and YAML emit listing projection
version 2: summary, summary source and state, answers and coverage, update
history, diagnostics, items, and sessions. A current orientation answer's
headline replaces `task.summary`. List never invokes inference or writes a
task folder. Documents are the registry; there is no stored master list.

A task with registrations ends in
`[working: <host> <state> <age>; ...]`. JSON and YAML carry `sessions` per
task. List only reads them.

`--scope all` scans direct, non-hidden project directories under each parent
in `~/.claude/task.local.yaml`'s `project_directories`. Entries may be absolute
or contain `${NAME}` / `%NAME%` environment references. Without configured
parents, DEVROOT is the single parent. With neither, the command errors and
names both options. Each project contributes tmp and dev/tasks; physical task
folders reached through shared links are emitted once under the shortest
project name.

Project and user scopes enumerate TASK ROOTS: folder crawl over tmp and
dev/tasks plus task_list references in markdown directly under those roots,
excluding parked `archived-tasks`. They do not crawl the whole project tree,
where examples are indistinguishable from live blocks. Reach a task_list in a
specific document with `--scope skill <name>` or `--scope file <path>`.

## show

Operation signature: `task.py show <ref> [--root PATH]`.

This is a pure task.yaml field read with no inference. An unresolvable ref or
unreadable local folder exits non-zero with a reason (archived, orphaned, or
remote). Output ends with one
`working: <host> <state> (<checkout>, session <id>, since <time>)` line per
registration, or `working: -`.

## items

Operation signature: `task.py items <ref> [--state S] [--priority P]
[--root PATH]`.

Requires an explicit ref and reads plan.md's `task_items` unit. It prints one
parseable `id  state  priority  title` line per item (absent priority `-`),
sorted by priority then block order. `--state` and `--priority` filter; states
are `available`, `in-flight`, `blocked-user`, and `deferred`. Empty output is
success. Block findings use stderr notes; validate is the findings gate.

An unresolvable ref or missing local directory exits non-zero with a reason.
Archived, orphaned, or remote refs may have no locally readable directory.

This answers "what else can I work on in this task?" For "what tasks are open
here?", use list. A pre-contract folder prints a note naming the one-time
conversion procedure.

Item edits are plan.md edits; there are no item CLI flags. Add, remove, or
re-state items by editing the block during update rotation. Completion is
REMOVAL from the block, and the block is the only place open work may live.

## sessions

Operation signatures:

- `task.py sessions [list] [--scope project|all] [--format text|json]
  [--root PATH]`
- `task.py sessions release <ref> [--all-local] [--root PATH]`
- `task.py sessions publish [--root PATH]`

Work creates a registration, and only release ends it. Harness exit and resume
do not. The record lives at `<task root>/.sessions/<host id>.yaml`, one file
per machine, committed and pushed with the task repository.

Only the writing machine can judge `running` or `not running`, using the exact
process stamp. Another machine reports `unverified`. Every display includes
record age.

Default list and `list` are read-only and offline. `release <ref>` removes the
calling session's registration and needs the Claude Code session; outside a
session, pass `--all-local` to remove every registration this machine holds
for the task. Both release forms publish. `publish` drains committed but
unpushed session files. A `not published` line means other machines cannot see
the change; when it names outside commits, commit and push the task root first.

Release another session's registration only when the user asks. Work keeps a
not-running registration from another session and names the release command.
