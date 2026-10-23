# Task review

## review

Operation signature: `task.py review [--scope all|user|project|skill|file]
[--target X] [--output PATH|-] [--no-open]
[--generate-missing-summaries | --no-generate-missing-summaries]
[--root PATH]`.

### Listing and grouping

Run the shared listing projection. Default to all scope: project directories
from `~/.claude/task.local.yaml`, else DEVROOT. With neither available, error
and name both options instead of narrowing to the current project. Explicit
`--scope project` renders only the current project's tasks.

Group active and blocked tasks as Open, deferred tasks as Deferred, closed
tasks as Closed, and omit archived tasks. Order projects by the newest dated
activity among open tasks; closed activity and summary dates do not count.
Projects without open activity sort last by name. Within each group, sort by
the latest log activity, newest first. Rows stay full brightness through four
hours and fade linearly to 33 percent at 120 hours and beyond. Rows do not
expand and there is no Updates section. Hover or keyboard focus on the task
name shows its summary card.

### Orientation answers

By default, answer the orientation question for every eligible local active,
blocked, deferred, or closed task whose answer is missing, stale, or invalid
before rendering. The answers engine runs one pass per repository and asks the
configured answer chain through llm-kit. It tries `local` entries first and uses
a `subscription` or `metered` class only when llm-kit's consent layer allows it for scope `task-kit`. Each answer is one file
under the task's `answers/`; review commits and publishes only this machine's
answer files and writes neither task.yaml nor log.md.

Use `--no-generate-missing-summaries` to inspect gaps without asking a model;
the positive flag explicitly selects the default. Model answering REFUSES when
llm-kit is absent or below the required version. Nothing substitutes, and all
other task verbs remain usable. Relay the refusal and its provisioning message
as printed; do not provision anything unasked.

Answering writes no log entry and does not advance last_update. A substantive
task change makes the stored fingerprint stale, so the next default review
answers it again.

### HTML output and outcomes

Write a self-contained HTML document and open it unless `--no-open` is set.
`--output -` writes HTML to stdout; otherwise use a private temporary file.
Report generation failures on stderr.

Missing or stale summaries remain visible with distinct warning treatment and
structured diagnostics. Unavailable, remote, invalid, or orphaned tasks are
shown as unavailable and never sent to a model. Answer states (current, stale,
missing, invalid, not-applicable) have distinct treatments.

Review exits 1 when it reports a model refusal or failed answer, and 0
otherwise; HTML is written either way. Errors and advisory `note:` lines for
uncommitted skipped folders, superseded answers, and answers awaiting publish
use stderr.
