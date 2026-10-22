# task-kit export protocol

`task-kit export` gives another program the facts of every task task-kit can
see, as JSON. It is the interface databench's tasks module reads. This page is
normative; `task-export.schema.json` beside it is the JSON Schema of each
document. task-kit works the same whether or not anything reads the export.

The export carries task FACTS: what each task folder, its validation, its
items and its working-session registrations say. Views built from them
(sections, needs, hand-off commands, item UI states, counts, filters) belong to
the consumer.

## Commands

```
task-kit export protocols --format json
    no discovery; kind "protocols", payload {"protocols": [1]}
task-kit export tasks --format json --protocol N [--parent DIR]...
task-kit export item --format json --protocol N --checkout ID --task PATH --item ID
```

`--parent DIR` (absolute, repeatable) names a directory whose child
directories are projects. Without it, task-kit uses its own configuration:
`project_directories` in `~/.claude/task.local.yaml`, else `$DEVROOT`.

## Output

- stdout: exactly one JSON document and a newline, for success and for every
  handled error. Nothing else is ever written to stdout.
- stderr: human diagnostics only. A consumer never parses it.
- Success: `{"protocol": N, "task_kit_version": "x.y.z", "kind": "<kind>", "payload": {...}}`
- Error: `{"protocol": N, "task_kit_version": "x.y.z", "error": {"code": "<code>", "message": "<text>"}}`
- `protocol` in an error document is the requested protocol when this
  task-kit serves it, else the highest protocol it serves.
- Exit codes: 0 ok; 1 internal (also `discovery_failed`); 2 usage; 3 not
  found; 4 protocol unsupported.
- Error codes: `usage`, `unknown_checkout`, `unknown_task`, `unknown_item`,
  `protocol_unsupported`, `discovery_failed`, `internal`; `answers` commands add `config_invalid`
  (exit 1).

## Negotiation and compatibility

- The consumer calls `export protocols` first, then passes the highest
  protocol both sides support. An unsupported `--protocol` exits 4, and the
  message lists the supported protocols.
- A task-kit that predates `export` answers `export protocols --format json`
  with exit 2 and an empty stdout. A current task-kit never does, because every
  handled outcome writes a document; that is how a consumer tells "too old"
  from "failed".
- Within a protocol, fields are only added. Enums are open: a consumer maps an
  unknown value to "other". A removed, renamed, retyped or newly required field
  takes the next protocol number, and the old protocol stays served for one
  task-kit minor release.

## Payload of `tasks` (protocol 1)

Every field is present; `?` marks a value that may be null.

| Field | Type |
|---|---|
| `generated_at` | string, ISO 8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`) |
| `revision` | string; changes whenever any field below changes |
| `repositories[]` | `{id: string, name: string, checkouts: [string]}`; one per origin, checkouts merged |
| `checkouts[]` | `{id: string, root: string (absolute), repository: string}` |
| `tasks[].checkout`, `.path`, `.stub`, `.location` | string; `path` is project-relative (`dev/tasks/<stub>`) |
| `tasks[].revision` | string; the content revision of the task's `task.yaml`, `plan.md` and `log.md` |
| `tasks[].title?`, `.priority?`, `.description?`, `.summary?` | string; `summary` is the headline of the task's orientation answer (current or stale) when it has one, else `task.summary` |
| `tasks[].summary_source?` | open enum: `"answer"` (the orientation answer's headline), `"task.yaml"` |
| `tasks[].answers` | `{<question id>: {state, answer?, answerer?, as_of?, fingerprint?}}`, one entry per question of the question catalog (empty when the catalog cannot be loaded) |
| `tasks[].coverage?` | `{catalog_version: integer, applicable: integer, current: integer}`: of the catalog's questions that apply to the task's classification, how many have a current answer; null when the catalog cannot be loaded |
| `tasks[].classification` | open enum: active, blocked, deferred, closed, archived, orphaned, remote, invalid |
| `tasks[].blocked_by` | [string] |
| `tasks[].last_activity?` | string, `YYYY-MM-DD HH:MM` of the newest update (a date-only entry reads as 12:00) |
| `tasks[].updates[]` | `{date: string, time?: string, detail: string}`, newest first, at most 20 |
| `tasks[].updates_total` | integer |
| `tasks[].has_items_block` | boolean |
| `tasks[].items[]` | `{id, title: string, state: open enum available, in-flight, blocked-user, deferred, priority?: string, note?: string}` |
| `tasks[].findings` | `{errors, warnings, item_errors, notes: [string]}` |
| `tasks[].sessions[]` | `{host, session_id, registered_at: string, state: open enum running, not running, unverified, local: boolean, recorded_at?: string, task_state?: string}` |
| `errors[]` | `{code: string, message: string, root?: string}`: discovery problems that did not stop the export |

Identity and grouping:

- A checkout id is the first 16 hex digits of SHA-256 over the checkout's
  canonical real path. A repository id is the first 16 hex digits of SHA-256
  over `repo:` plus its normalized `origin` URL (`path:<real path>` for a
  checkout without one).
- A task folder reached through several checkouts is listed once, under the
  checkout with the shortest directory name (the rule `task list --scope all`
  uses).
- `tmp/` tasks are not exported.
- Order: repositories by name, checkouts by root, tasks by repository name,
  then path, then checkout root.
- `errors[].code` is an open enum. task-kit 0.15.0 emits `unreadable`,
  `repo_identity` and `sessions`; task-kit 0.17.0, which adds `tasks[].answers`, also
  emits `answers` and `questions`.

Answers (`tasks[].answers`):

- A question is declared data (id, version, the inputs it reads, an answer
  schema, the classifications it applies to); the catalog is the shipped
  questions plus a fleet layer. An answer is current when the fingerprint it
  stored equals the fingerprint of the question definition plus its inputs now.
- `state` is an open enum: `current`, `stale` (an answer exists for older
  inputs), `missing`, `invalid` (answer files exist and none is usable) and
  `not-applicable` (the question does not apply to the task's classification).
  An unusable answer file is also an `errors[]` entry with code `answers`,
  naming the file; it never stops the export.
- `answer` is the chosen answer, an object shaped by the question's answer
  schema, null for `missing`, `invalid` and `not-applicable`. Any machine
  chooses the same one among the records of several machines, using nothing
  machine-local.
- `answerer` is `{kind: "model" | "script", entry?, name?, tier?, effort?,
  attempts: [{entry, outcome}]}`: a model answer names its endpoint entry, cost
  tier and effort; a script answer names the script. `as_of` is
  `{task_commit?, code_commit?}`, the commits whose content the answer
  describes. `fingerprint` is the inputs fingerprint a model answer stored,
  null for a script answer.
- A script answer is computed when the export runs, from the task folder's git
  history and the clock, and is never stored. The shipped script questions are
  `activity` (`days_since`, `last_at`, `last_entry`) and `task_commits`
  (`commits`: `{sha, date, subject}`, newest first, at most 10).

## Payload of `item`

`{checkout, path, item: <a tasks[].items[] record>, plan_section?: string,
plan_section_truncated: boolean}`. `plan_section` is the body of the item's
plan.md section (the heading whose first token is the item id), at most 8000
characters; null when there is no such section. The item must belong to a
task the `tasks` export lists under that checkout: an unknown checkout, task
or item answers `unknown_checkout`, `unknown_task` or `unknown_item` (exit 3).

## Answers commands

`task-kit answers` computes and serves the answers of the question catalog. It follows the rules of
"Output", "Negotiation and compatibility" and the exit codes above: exactly one JSON document on stdout, human
diagnostics on stderr, `--protocol N` required, an unsupported protocol answered with exit 4. These documents
are not covered by `task-export.schema.json`; this section is their contract.

```
task-kit answers refresh --format json --protocol N [--parent DIR]... [--allow CLASSES] [--batch N]
                         [--stop-file PATH] [--deadline-s N]
task-kit answers status  --format json --protocol N [--parent DIR]...
task-kit answers prune   --format json --protocol N [--parent DIR]...
```

- `--parent` is as for `export tasks`. Tasks are grouped by the git repository that holds the task folder; a
  folder outside git is an `errors[]` entry with code `answers`.
- `--allow CLASSES` is a comma-separated set of llm-kit cost classes (`free`, `usage`, `spend`) the pass may
  use; `free` is always allowed. Without it, the classes are llm-kit's per-machine consent, read once, the first
  time a pass has a model to ask. Free entries are always tried before the others.
- `--batch N` (default 4, at least 1) is the number of TASKS one pass answers per repository, most recently
  modified first (the newest commit touching the task folder outside `answers/`); each kept task has every
  pending question answered.
- `--stop-file PATH`: when the file exists, the pass stops between phases. `--deadline-s N` (at least 0) stops it
  the same way N seconds after it starts. A stop is checked before each phase (prepare, answer, commit) and,
  while answering, before each question; answers already finished are still committed.
- Exit codes: 0 ok (a skipped repository, a refusal and per-task failures are reported in the payload, not as an
  error); 1 `internal`, `discovery_failed` and `config_invalid` (the question catalog, the answers configuration,
  the data directory or this machine's identity cannot be loaded; the message names the file); 2 `usage`; 4
  `protocol_unsupported`.

A pass, per repository: skip it when HEAD is detached or a git operation is in progress; under the repository
lock commit this machine's uncommitted answer files, work out the pending questions, publish when something is
pending or unpublished, and cut the batch; answer without the lock; under the lock revalidate (a task that
changed meanwhile is dropped as superseded), write this machine's answer files, commit exactly them, publish.
The lock is never held across a model call.

`refresh` (kind `answers-pass`), payload:

| Field | Type |
|---|---|
| `allow?` | `{classes: [string], source: "explicit" \| "consent", note: string}`; null when no pass needed a model |
| `stopped?` | `"stop"` or `"deadline"` when a stop request or the deadline ended the run, else null |
| `repositories[]` | one report per repository (below); a stop ends the run after the repository it stopped in |
| `errors[]` | as in the `tasks` payload |

A repository report: `repo` (absolute path), `outcome` (open enum: `completed`, `skipped`, `stopped`,
`refused`), `reason?` (why it was skipped), `stop?`, `stopped_before?` (the phase it did not start: `prepare`,
`answer` or `commit`), `phase?` (the last phase begun), `committed_dirty[]` (answer files committed from an
earlier pass), `pending` (tasks with work), `kept[]` (the batch, project-relative folders), `answered[]`
(`{task, question, entry, tier}`), `dropped[]` (`{task, question, reason}`: superseded or unwritten),
`failed[]` (`{task, question, reason, attempts: [{entry, outcome}]}`), `unattempted[]` (`{task, question}`),
`skipped_tasks[]` (`{task, reason}`: for example uncommitted changes outside `answers/`), `commit?` (sha),
`commit_error?` (written files stay for the next pass), `publish[]` (`{status, message, committed, published,
attempts}` of each publish), `unpublished` (this machine's committed files not yet on the upstream, judged
against the last fetch), `refusal?` (`{state: "absent" or "too-old", message}`: llm-kit cannot answer; nothing
is substituted and the message names the install or update command), `notes[]`.

`status` (kind `answers-status`) writes nothing, takes no lock and runs git without optional locks. Payload:
`catalog_version`, `questions[]` (ids), `model_answering` (`{available, state: "ok", "absent" or "too-old",
message?}`), `totals` (`{tasks, applicable, current, pending_tasks, pending_questions, unpublished}`),
`repositories[]` and `errors[]`. A repository entry: `repo`, `skip_reason?` (why a pass would skip it),
`pending_tasks`, `pending_questions`, `own_uncommitted[]`, `unpublished[]`, `notes[]` and `tasks[]`, each
`{task, status, skip?, states: {<question id>: state}, coverage?: {catalog_version, applicable, current},
pending[], notes[]}`.

`prune` (kind `answers-prune`) deletes this machine's answer files of questions the catalog no longer holds
(retired or unknown ids), commits the deletions and publishes them; another machine's file is never touched.
Payload: `repositories[]` of `{repo, outcome, reason?, pruned[], refused: [{path, reason}], commit?,
commit_error?, publish[]}`, and `errors[]`.

## What the export never does

It writes nothing: no task folder, no data directory file, and no git index
refresh (its git commands run with `GIT_OPTIONAL_LOCKS=0`). It never imports
the dashboard's code.
