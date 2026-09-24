# Task triage protocol

Use this protocol for the task skill's agent-side `triage` capability. It has
no CLI verb. The task skill always emits `orchestrate`; triage runs under that
orchestration path.

## Trigger and scope

- `triage the tasks` or `/task triage` with no ref means every open and
  deferred task in the project.
- `/task triage <ref>` or `triage <task>` means only that task.

For a project-wide triage, prepare all audits before presenting any ruling. For
a specific task, use the same protocol for that task only.

## 1. Select

This step is project-wide only. Run `task.py list`. Select every open task
(active or blocked) and every deferred task; no age or last-updated threshold
of any kind. An empty scaffold (`task_items` with `items: []` and placeholder
documents) needs no audit: classify it directly as `never-started` and
recommend archive.

Selection is complete when every open and deferred task is either selected
for audit or classified as an empty scaffold.

Audits are prepared for all selected tasks. Presentation order defaults to
oldest first -- longest since last update, using the `last_update` column
`task.py list` prints (a task with no date `-` sorts first; deferred tasks
appear in `list`'s Deferred tasks: section with the same column and sort into
the same order). The user may name another order.

## 2. Audit

Launch one read-only audit brief per selected task in parallel. Each brief is a
standalone unit and reads the task folder's `CLAUDE.md`, `plan.md` and its
`task_items` block, `log.md`, and `task.yaml`.

Verify **every** open item against live repository state. Use git history since
the task's last dated log entry, grep or equivalent searches for named files and
functions, and direct checks of whether described problems still exist. Cite
evidence for each item: commit, `file:line`, or command output. Flag stale
claims anywhere in the task folder. Check overlap with other tasks in the same
project and with the task repository's other projects, including work that
duplicates, supersedes, or should absorb this task.

Use the model and effort the user names. Otherwise use orchestrate's routing.
The tested run used Codex Luna at xhigh effort, a read-only sandbox, and wrote
each result to an output file with `-o`.

Each brief must return:

1. Purpose.
2. A verdict for every open item: `done`, `obsolete`, `open`, or `unclear`,
   with evidence.
3. Remaining work, sized and stating its value.
4. Two or three options with consequences, drawn from the dispositions in
   step 4 (archive, close, defer, update, merge, reviewed-keep).
5. Outcomes for the task's premises.

The audit is complete only when every open item has a verdict and evidence,
stale claims and overlap have been checked, and the brief contains all five
return sections.

## 3. Present

The orchestrator verifies each audit and forms its own recommendation. Present
one task at a time, in presentation order (oldest first by default, per step
1, or the order the user named). Lead with the recommendation, then give a
more detailed summary of the task and remaining work in no more than 200
words, then present the options and consequences.

Wait for the user's ruling for that task. The disposition is the user's
decision. The user may answer for several presented tasks at once. Answer
follow-up questions from the audit plus a bounded read; do not reopen the
whole project without a reason.

Presentation is complete when the user has a ruling for the current task or
has explicitly deferred the decision.

## 4. Apply the ruling

Apply only the user's ruling. Commit and push the task repository after each
applied ruling.

1. **archive** -- run `task.py archive <ref>`. For `dev/tasks`, this commits
   the final state and removal.
2. **close** -- run `task.py close <ref>`. Keep the task folder.
3. **defer** -- run `task.py update <ref> --status deferred`. Keep the task
   folder; commit and push the task repository. Use this for a task that is
   purposefully put on hold, intended to be resumed later -- not done, not
   abandoned, just not now. `task.py reopen <ref>` brings it back to active.
4. **update** -- rotate the folder per `handoff-template.md`: remove done and
   obsolete items, fix stale claims, then run `task.py update <ref>`.
5. **merge** -- copy durable documents into the absorbing task, such as a
   `<topic>/` subfolder; add surviving items to its `task_items` block using
   the user's framing (for example, P1 active rather than deferred when the
   user says so); reference the item ids from its `CLAUDE.md` priorities;
   append a dated log bullet naming what was dropped and why; commit; then
   archive the source task.
6. **reviewed-keep** -- run `task.py update <ref>` with no field edits. This
   appends `refresh (no field edits)` so `list` shows the task as recently
   reviewed. Commit and push.

After applying a ruling, verify the task repository state and report any open
defect that the ruling leaves unowned. In particular, archiving a task can
leave live work without an owner; name that work so it can be re-homed.

## Gotchas

- An edit brief for an existing task must say: **do not modify existing log or
  plan lines**. A "keep ASCII" instruction was once read as permission to
  rewrite a historical log line whose non-ASCII character was the point of
  the entry.
- A worker's claim that a dependency is missing is a hypothesis. Reproduce it
  outside the worker's sandbox before acting on it.
- Archiving a task can leave live work unowned. Name any open defect the
  archived task held when reporting the archive, so the user can re-home it.
