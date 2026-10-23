# Core contracts

Load this reference for the expanded task model, validation behavior, cwd and
reference resolution, or provider-wide gotchas.

## Data model

The task **folder** is the source of truth -- task-level state lives in its
`task.yaml`; the open **items** (the enumerable units of next work within the
task; synonym "work item") live in plan.md's embedded `task_items:` block,
and ONLY there (completion = removal; CLAUDE.md priorities reference items by
id, never restating state). **References** (`task_list:` blocks embedded in
any markdown doc) are inert pointers: `{path, host?}`, no status, resolved by
reading the folder. The task **id is its path** (`tmp/<stub>` or
`dev/tasks/<stub>`), so `move` must rewrite every reference. Every operation
names its task explicitly.

**Durability is location.** `tmp/<stub>` is ephemeral and machine-local;
`dev/tasks/<stub>` is version-controlled. Archiving a dev/tasks folder records
its final state in version control, then deletes it, because version control is
the record. Git is the VCS the scripts detect and automate; in a workspace
under another VCS, the scripts run no git commands and leave submission to the
agent.

Archiving a tmp folder parks it at `tmp/archived-tasks/<stub>`; so does a
dev/tasks folder when git ignores EVERY file in it, at
`dev/tasks/archived-tasks/<stub>`. Version control holds no copy of either
parked folder. A dev/tasks folder git holds only PARTLY is kept in place:
moving it would take tracked files off their tracked paths with no commit.

The unit below the task is an ITEM (long form "task item"; accepted synonym
"work item"). Route "work items", "open items", "goals", "sub-tasks", and
"what is available on this task" to `items`. Never introduce a sub-task
entity: an item has no folder, lifecycle verbs, or outside references. Promote
an item that outgrows the block to a task (`init` plus a `task_list` ref).

## Cwd and reference resolution

Refs resolve against `--root`, which defaults to cwd. A `cd` into the task
folder, or into the repository behind a `dev/tasks` link, breaks every
ref-taking verb: a bare stub fails with "matches no task folder under tmp/ or
dev/tasks/", and a path ref fails with "not a known task location". Run from
the project root, or pass `--root <project>` when the shell must sit elsewhere.

`<ref>` is a path (`tmp/<stub>`, `dev/tasks/<stub>`) or a bare stub. An
ambiguous stub is an error that lists candidates. The project root remains the
PROJECT even when `dev/tasks` links into another repository; it is never the
link target.

## Validation behavior

Validation gates work with BOTH errors and warnings. An uncommitted dev/tasks
folder blocks work until committed, because durable work that exists only in
the working tree is one removal away from gone. The check is git-scoped but
the posture is VCS-neutral: outside a git repo, or where the folder is
git-ignored, it is an advisory note because the scripts cannot check another
VCS. Document size budgets follow the same posture: over the ceiling blocks
work until the document is decomposed under the folder-authoring contract.

Fix findings forward; there is no recovery path. Advisory `note:` lines do not
affect the exit code. `approaching-budget`, `dominant-section`, and
`session-diary` notes require attention at the next rotation.
`version-control-unverified` means submit with the workspace VCS;
`version-control-ignored` means git will never carry the folder. An
`invalid answer file:` note is advisory; the answers engine rewrites its own
invalid file on the next pass.

## Shared gotchas

- References never carry status. To answer "is X done?", resolve the folder
  with `show`, `validate`, or `list`; do not infer from a task_list entry.
- Native TaskCreate/TodoWrite records are session-scoped harness state. They do
  not interoperate with durable task folders, references, validation, or the
  lifecycle here.
- `hand-off` names the v1 task TYPE and the user-invoked packaging capability;
  neither is a CLI verb. Do not change `type: hand-off` to `type: task`.
- Use the explicit plugin-venv interpreter. The CLI self-repairs by re-execing
  under its provisioned venv, but the documented path is the canonical,
  cwd-independent invocation.
