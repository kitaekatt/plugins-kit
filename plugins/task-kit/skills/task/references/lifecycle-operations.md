# Lifecycle operations

Load the section selected by the capability record. These mutations preserve
the task-folder and task-list contracts.

## init

`task.py init <stub|desc> [--dest tmp|dev/tasks] [--type hand-off] [--root PATH]`

Output is always a valid active task. On any validation failure, init removes
the folder and exits non-zero; it never leaves a partial or invalid folder.
It prints the absolute folder path on success.

The default destination is tmp (ephemeral). An existing target path errors --
use update, not init. A freeform description derives the stub; a path-shaped
argument is sanitized, never treated as a path. Scope the stub to the work
unit (phase or project), never the session: `atoms-17-phase2`, not `atoms-22`.
After init, the folder holds placeholder scaffolds; filling them in is agent
work under the folder-authoring contract.

## validate

`task.py validate <ref> [--root PATH]`

Prints the classification (active, blocked, closed, deferred, archived,
invalid, or remote) to stdout and errors AND warnings to stderr. Exit 0 means
zero findings. Warnings include an uncommitted dev/tasks folder, dangling
`depends_on` or `blocked_by`, an orphaned tmp ref, no `task_items` block, a
stale item reference, or a document over its line ceiling.

Findings gate work: errors AND warnings both block. Fix forward, then
re-validate. Advisory `note:` lines do not affect the exit code; their shared
meanings are in the core contracts.

## update

`task.py update <ref> [--status S] [--priority P] [--description D]
[--depends-on PATH ...] [--blocked-by PATH ...] [--agent-hint H]
[--skill-to-invoke NAME ...] [--durable-output PATH ...] [--summary S]
[--hidden-until DATE|none] [--root PATH]`

Requires an explicit ref and initializes the folder when absent (upsert).
Applies field edits, appends one dated entry to log.md, re-validates, and
prints the classification. Exit 0 means no findings, but the write persists
regardless (fix forward). List-valued flags are repeatable and REPLACE the
stored list. `--summary` writes legacy `task.summary`; an orientation answer's
headline supersedes it in list and review.

`--hidden-until YYYY-MM-DD` hides the task from list, review and the other
task menus until that date (see core-contracts.md, "Hidden until"); `--hidden-until
none` removes the field. A bad date exits non-zero before anything is written
or scaffolded.

`--status deferred` means purposefully on hold and intended to resume. It is
neither open nor closed. Deferred tasks leave list's Open section and review's
Open group for their Deferred sections. `reopen <ref>` returns one to active.

The script's only document write is the dated log.md line; it never rewrites
plan.md. On every substantive update, run the rotation, stale-state,
durable-outputs, and vocabulary passes. Rotation is standing discipline, not
a cleanup that starts only after a budget is exceeded. For a pre-contract
folder, start with the one-time conversion and run validate until clean; the
`task_items` block must end as the ONLY carrier of open-work state.

A folder document that would outlive the folder belongs in the repository it
describes. Move it there immediately and declare it with repeatable
`--durable-output <repo-relative path>` (which REPLACES the list). Judge at
authoring time; archive's check is only a backstop. Declaring nothing is valid.

Update never packages a folder for a fresh session and never emits the
hand-off baton. Only a user invocation of `/task hand-off` or `/restart
hand-off` enters that capability. A natural stopping point is not an
invocation; finish the turn normally.

## close

`task.py close <ref> [--root PATH]`

Requires an existing folder whose stored status is active. Sets
`status: closed`, keeps the folder, and prints `closed: <id>`.

### Optional pre-close command

The repository's `.claude/bootstrap.json` can configure `task_kit.pre_close`
as a nonempty argv string list or an object with `argv` and `required_paths`.
Both close and archive run this command before they change files.
Archive also runs it before repair of an interrupted archive. The default is no command.

The command runs in the absolute project root, without a shell. Whole arguments
`{task}`, `{task_id}`, and `{repo}` become the canonical task ref, the folder slug,
and the absolute project root. Other arguments stay literal. Environment-variable
and shell expansion do not occur.

For git-kit's checker, the command arguments after its interpreter and script are
`check-task-close --repo {repo} --task {task_id} --json`. The script and interpreter
entries name the installed absolute paths. The `{task_id}` form matches a take
whose `--task` names the folder slug. Use `{task}` when takes store canonical refs.
The object form can name the git-kit script in `required_paths`.
This optional list contains absolute literal paths. Tokens and environment
variables do not expand there. Unknown object keys are errors.

Exit 0 requires a JSON object with `allowed` (boolean) and `parked` (object list).
Only `allowed: true` with an empty `parked` list permits the operation. A refusal
shows the returned object. Invalid JSON, an invalid response shape, nonzero exit,
and a timeout after 120 seconds block the operation. Task files stay unchanged.

If the command is absent, task-kit uses its normal close and archive behavior.
If a configured executable is missing, task-kit reports `check omitted` on stderr
and uses that same behavior. A missing `required_paths` entry has the same outcome.
This guard covers an absent plugin script when its interpreter still exists.
Unreadable required paths block the operation. The hook adds no git-kit import
or installation requirement. A configured checker error remains an error.

## reopen

`task.py reopen <ref> [--root PATH]`

Allowed only while the folder exists, including a parked archived folder at
`<location>/archived-tasks/<stub>`, which reopen restores to
`<location>/<stub>` first. A task with no live or parked folder is gone and
cannot be reopened. Reopen sets `status: active`, re-validates, prints the
classification, and exits according to findings.

The prior stored status is not inspected: closed, deferred, and restored
archived folders reopen the same way.

## archive

`task.py archive <ref> [--root PATH]`

Acts on an ACTIVE task; closed or deferred tasks get a reopen-first error.
The optional pre-close command also applies to archive.
For tmp, it sets archived and parks the folder under `tmp/archived-tasks/`.
For dev/tasks in git, it commits final state, deletes the folder, and commits
the removal -- two commits scoped to the task folder, with final state safely
recorded before removal. Outside git, it records final state and keeps the
folder as `vcs_pending`; submit with the workspace VCS, then run delete.

When git ignores EVERY file in a dev/tasks folder, archive treats it as local
scratch and parks it under `dev/tasks/archived-tasks/`; version control holds
no recovery copy. When git holds only SOME files, archive keeps the folder in
place and names the tracked and ignored sets. This is supported, and it is the
archive disposition where folder contents are unrecoverable: relocate durable
outputs before delete.

The durable-outputs check runs before anything is parked, committed, or
removed. Every declared path must exist and live outside the task folder.
Either failure refuses archive and names every offender. No
`durable_outputs` field produces a reminder but archive proceeds. On refusal,
relocate each document, redeclare it with update, then archive.

## delete

`task.py delete <ref> [--root PATH]`

Acts on an ACTIVE or ARCHIVED task. It finds a parked copy from the live ref.
Closed or deferred tasks get a reopen-first error. It refuses a dev/tasks
folder that git can see is dirty; delete never auto-commits, so use archive.
Outside git, and for a parked folder, no VCS check applies and removal is
unrecoverable. It removes the folder even for tmp and prints `deleted: <id>`.

## move

`task.py move <ref> <tmp|dev/tasks> [--root PATH]`

Because id equals path, move REWRITES every task_list reference to the old
path across all `*.md` under the project root. Rewrites are span-precise;
prose mentions and other refs are untouched. It prints
`moved: <old> -> <new>` plus the rewritten-document count. The stub is
preserved. It refuses an occupied destination or an absent or remote folder.
