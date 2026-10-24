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
| 1 | `internal`, `publication_failed` |
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
| `item-complete` | Required `item` id and `summary`. Removes the open item and records its accomplishment. |
| `session-dismiss` | Required exact `host`, `session_id`, `registered_at` tuple belonging to this task. |
| `accept-defaults` | Required non-empty `defaults` list of exact current grounded decision records, each with a default. |

Item states remain available, in-flight, blocked-user and deferred.
Priority is P1, P2 or P3. An item priority can be cleared with null.
Titles are non-empty single lines. Item details cannot close the surrounding section or leave a fence open.
Acceptance commands are non-empty lists of non-empty string argv lists.

Success payload: `{task,revision,action,item?}`. Session dismissal also carries its publication result.
Semantic task edits update the canonical files and log. They do not commit arbitrary project code.
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
