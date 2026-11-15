# Task manager protocol

`task-kit manage` serves full task input and semantic mutations as JSON.
It works without Databench or llm-kit. Existing task verbs and export fields keep their contracts.

## Commands and negotiation

```
task-kit manage protocols --format json
task-kit manage input --format json --protocol N --project ABS --task dev/tasks/STUB [--item ID]
task-kit manage mutate --format json --protocol N --project ABS
```

The consumer probes `protocols` before other commands. Its payload contains
`protocols`, `actions`, `input_limit` (bytes) and `revision_files`.
Protocol 1 is additive. Removed or retyped fields require a new protocol.
The project is an existing absolute checkout directory selected by the host.
The host resolves client identities through its authoritative project inventory.

Every handled outcome writes one JSON document and a newline to stdout.
Diagnostics and unexpected exception tracebacks go to stderr.
Success: `{protocol,task_kit_version,kind,payload}`.
Error: `{protocol,task_kit_version,error:{code,message,written}}`.
`written` names files already changed, including partial mutations. Startup errors can omit it.
Kinds are `manage-protocols`, `manage-input` and `manage-mutate`.

| Exit | Error codes |
| --- | --- |
| 0 | Success |
| 1 | `internal`, `publication_failed`, `commit_failed` |
| 2 | `usage` |
| 3 | `unknown_task`, `unknown_item` |
| 4 | `protocol_unsupported` |
| 5 | `conflict` |
| 6 | `refused`, `held`, `too_large` |
| 7 | `busy` |

Consumers inspect the code and report the reason. They never retry a mutation silently.

## Full input

The input payload contains these fields:

| Field | Meaning |
| --- | --- |
| `project_root` | Canonical checkout directory. Acceptance argv uses this working directory. |
| `checkout`, `repository` | The same identity algorithms as the facts export. Repository identity merges aliases and checkouts. |
| `task` | Export task facts, plus `folder`, `status`, full `plan`, `log`, `context` and full raw `items`. |
| `task.revision` | Opaque manage revision over task.yaml, plan.md, log.md and CLAUDE.md. It differs from the export revision. |
| `task.findings` | Canonical validation errors, warnings, item errors and notes. |
| `task.sessions` | Current session evidence, using the export record shape. |
| `item` | The selected raw item, including extension fields such as `accept`. Null without `--item`. |
| `instructions` | The complete keyed item section. Null without `--item`. |
| `acceptance_argv` | Checked argv lists from the item's optional `accept` field. Empty when absent. No shell interpretation. |
| `git` | `{vcs,head,branch,status}` from the selected checkout. Status is porcelain lines. |

An item section must exist exactly once and contain text. Invalid source refuses.
The combined source limit is 1048576 bytes. Oversized input refuses instead of truncating.
Without `--item`, input inspects post-worker task state even after an item disappears.
Validation warnings remain visible. The manager applies its job-readiness rules before launch.
The reader takes no optional Git locks and generates no answers or summaries.

## Mutation request

The mutate command reads one strict JSON object from stdin:

```
{"task":"dev/tasks/example","expected_revision":"opaque-token","action":"task-update","fields":{"description":"Updated"}}
```

All four keys are required. Unknown request or action field keys refuse.
Duplicate JSON keys and non-finite numbers refuse. The request limit is 1048576 bytes.
`expected_revision` is the latest manage input token. Creation uses null for expected absence.

| Action | Fields |
| --- | --- |
| `task-create` | Required `title`. Optional `priority`, `description`, `first_item` (an item-add fields object). The task path supplies the stub. |
| `task-update` | Any of `title`, `priority`, `description`, `status`. Status is active, blocked, deferred or closed. |
| `item-add` | Required `title`. Optional `priority`, `details`, `note`, `acceptance_argv`. The id derives from the title. |
| `item-update` | Required `item` id. Optional `title`, `priority`, `note`, `state`, `acceptance_argv`. |
| `item-complete` | Required `item` id and `summary`. Removes the open item and records its accomplishment. Also removes the item's prose section from `plan.md` when a heading is exactly `### <item id>`: the section runs through the next heading of the same or a higher level, so lower-level headings inside it go too. A heading with anything after the id (`### <id> -- title`), another level, a heading inside a code fence, or more than one exact heading leaves `plan.md` prose alone. |
| `session-dismiss` | Required exact `host`, `session_id`, `registered_at` tuple belonging to this task. |
| `accept-defaults` | Required non-empty `defaults` list of exact current grounded decision records, each with a default. |

Item states remain available, in-flight, blocked-user and deferred.
Priority is P1, P2 or P3. An item priority can be cleared with null.
Titles are non-empty single lines. Item details cannot close the surrounding section or leave a fence open.
Acceptance commands are non-empty lists of non-empty string argv lists.

Success payload: `{task,revision,action,item?,commit}`. Session dismissal also carries its publication result.
Semantic task edits update the canonical files and log. They do not commit arbitrary project code.
Every action except `session-dismiss` commits the task files it wrote (`task.yaml`, `plan.md`, `log.md`; all four files for `task-create`) in the repository that physically holds the task folder, so a linked `dev/tasks` root commits in the linked tasks repository and never in the host project.
The commit is limited to those files (`git commit --only`): other staged content stays staged and other working-tree edits stay out.
The message is `task manage: <task> <action>` plus the item id when the request names one, for example `task manage: dev/tasks/example item-complete write-schema`.
Mutate actions other than `session-dismiss` never push. `commit` is the sha of the commit made, or null when the folder is in no git repository, git ignores every written file, or the machine turned committing off.
`~/.claude/task.local.yaml` takes `manage: {commit: false}` to write edits without committing them; the default is `true`. An unknown `manage` key or a non-boolean `commit` refuses (`refused`) before any write.
A git failure (a failing hook, a held index lock, a repository probe git cannot answer such as dubious ownership; only "not a git repository" counts as no repository) returns the error `commit_failed` with the written files in `written`. The edit has already happened and stays on disk; read the task again, then commit or revert it.
`session-dismiss` uses the session publisher, which commits and pushes the session file.
Session dismissal uses the existing session store and publisher. An unpublished dismissal reports an explicit failure and its written files.

## Concurrency and boundaries

Managers serialize mutations across processes by the physical task repository.
A linked `dev/tasks` root works. Task paths, files and subdirectories below it must be unlinked and contained.
The writer compares the expected revision under that lock and checks it again before replacement.
A verified local running session holds semantic edits. Unverified records never hold them.
A dismissal targets an exact registration and never edits the other machine's file.

Each file replacement is atomic. A multi-file mutation can fail after earlier replacements.
The error names the completed writes, so a consumer reads the task again before inspection or recovery.
Harness editors do not take this lock. [The mutator](../scripts/task_system/mutation.py) documents the remaining final compare/rename race.

The host independently validates the structured worker receipt, actual task/item and Git state, and acceptance commands.
It then supplies the validated post-worker revision to `item-complete`.
An intervening edit refuses reconciliation. A process exit zero or completion claim alone does not establish success.
