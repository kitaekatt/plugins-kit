# Background workflows

Load the selected section for queueing or task-status summarization. Both
capabilities protect the main thread by dispatching work elsewhere.

## queue

Queue means a unit of work to do when capacity allows. Four rules define it:

- **Unattended.** The unit completes end to end with no user involvement. A
  unit that needs a user decision, access, or approval is not queueable.
- **Clarified first.** Ask whatever the unit needs to run unattended before
  queueing it. Nothing with an open question enters the queue.
- **In parallel.** Queued work runs beside other work, or alone when there is
  none.
- **Never in the main thread.** The session that took the request does not do
  the work itself.

Clarify scope, inputs, access, and the check that proves completion. Batch the
questions in one message and ask only what the unit cannot run without.

When the work belongs to a dev/tasks task, make it an item: add the plan.md
`task_items` entry and its `### <id> -- <title>` detail section, including
`accept:` when a check exists. The CLI has no queue verb.

Launch a background worker with the clarified brief under orchestrate and
keep the main thread free. Never run the queued unit inline, and never invoke
task work merely to execute its queued item in the main thread. Queue adds no
task verb, route, or state.

## status

Operation signature: `task.py status <ref> [--root PATH]`.

Run the verb. It prints SUBSTRATE only: classification, findings, task.yaml
fields, paths for CLAUDE.md, plan.md, and log.md, and the parsed task-items
menu.

Status is the system's ONE inference verb. Dispatch a BACKGROUND summarizer
through orchestrate to read the substrate's documents and produce the human
summary. Do NOT read plan.md or log.md and summarize inline in the main
context. Lead with the parsed open-item menu from the substrate without
re-parsing it. Use the model orchestrate's rendered routing policy resolves;
do not choose or assume one here.

The substrate includes a `sessions:` block or `sessions: none`. Fold working
registrations into the summary. Summarizing inline defeats the capability's
reason for existing: context preservation.
