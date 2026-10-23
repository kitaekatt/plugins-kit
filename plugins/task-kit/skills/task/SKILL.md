---
_schema_version: 1
name: task
author: christina
skill-type: capability-skill
description: Use when creating, listing, working, queueing, closing, archiving or moving task folders and task_list refs. Do NOT use for TaskCreate/TodoWrite or code review.
---

# Task

Routes 16 task.py verbs plus queue, status, dashboard, triage, and hand-off.

## Data model and CLI

The folder is the source of truth: state in `task.yaml`, open items only in
plan.md's `task_items:` block, inert pointers in `task_list:` blocks, and id as
the `tmp/<stub>` or `dev/tasks/<stub>` path. Every operation names its task.
Expanded contracts: `references/core-contracts.md`.

Run from the project root. Use the plugin venv's Python explicitly -- not `uv run python`, which resolves the wrong environment from a foreign cwd:

```bash
# macOS/Linux (Windows: .venv/Scripts/python.exe instead of .venv/bin/python)
"${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}"/plugins-kit/task-kit/.venv/bin/python \
  "${CLAUDE_SKILL_DIR}/scripts/task.py" <verb> [args]
```

`operation:` abbreviates this as `task.py`. Exit 0 succeeds; non-zero blocks.
Diagnostics go to stderr, results to stdout. `<ref>` is a path or bare stub.

```yaml
capability_skill:
  _schema_version: "1"
  identity: "Route task.py operations and agent-side behaviors."
  scope:
    covers: [task-folder operations, task items, task_list references, agent-side task workflows]
    excludes: [native TaskCreate or TodoWrite tracking, code review, task-system design changes]
  external_capability:
    kind: tool
    name: task.py (task-system CLI)
    description: "CLI over task folders, records, items, answers, and sessions."
  layering:
    claude_md: []
    skill_md: [data model, CLI invocation, 20 capability routes, immediate agent gates]
    references: [procedures, edge cases, authoring and review protocols]
  capabilities:
    - id: init
      keywords: [create task, new task, init, scaffold folder, start tracking]
      user_objective: "Create a new task folder with scaffolding, seeded from the current request."
      operation: task.py init <stub|desc> [--dest tmp|dev/tasks] [--type hand-off] [--root PATH]
      details: references/lifecycle-operations.md#init
    - id: validate
      keywords: [validate task, check task, findings, classification, schema check]
      user_objective: "Check a task against its type schema and structural rules; classify it."
      operation: task.py validate <ref> [--root PATH]
      details: references/lifecycle-operations.md#validate
    - id: work
      keywords: [work on task, start task, dispatch]
      user_objective: "Work an explicitly named task and load its working context."
      operation: task.py work <ref> [--init] [--root PATH]
      details: references/work-and-hand-off.md#work
      steps:
        - n: 1
          action: "Run the work verb. Any validation error or warning blocks. On success it registers this session when enabled; relay each `session:` line in one sentence."
        - n: 2
          action: "Immediately invoke EVERY emitted Skill(...) line via the Skill tool in printed order, before other tools. Obey the closing directive and START through orchestrate in this turn, honoring eligible agent_hint."
    - id: update
      keywords: [update task, edit fields, refresh folder, set status, set priority, rotation]
      user_objective: "Upsert a task and apply task.yaml field edits; refresh the folder."
      operation: task.py update <ref> [--status S] [--priority P] [--description D] [--depends-on PATH ...] [--blocked-by PATH ...] [--agent-hint H] [--skill-to-invoke NAME ...] [--durable-output PATH ...] [--summary S] [--root PATH]
      details: references/lifecycle-operations.md#update
      gotchas:
        - "For substantive progress, run handoff-template.md's four update passes; convert a pre-contract folder first."
    - id: hand_off
      keywords: [hand-off, hand off, package for a fresh session, resume in a new session, baton, continue in a new session]
      user_objective: "Package the folder so a fresh session resumes with one paste -- ONLY when the user invokes /task hand-off or /restart hand-off."
      operation: "/task hand-off <ref> or /restart hand-off [<ref>] -- user-invoked; no CLI verb. Dispatches onto task.py update <ref> and task.py sessions release <ref> plus the agent-side packaging in the steps."
      details: references/work-and-hand-off.md#hand_off
      steps:
        - n: 1
          action: "GATE: proceed only if the user's own message invoked /task hand-off <ref> or /restart hand-off [<ref>], or said in plain words to hand off / package the task for a new session. Nothing else qualifies: not an update that looks like the session's last, not a stopping point you judge natural, not the end of a long turn, and not the template's 'end the turn with the baton' (that instruction is scoped to THIS capability). Without the invocation there is no hand-off -- finish the turn normally and do not raise the idea yourself. When relaunch-kit:restart invoked this capability, its request_restart call comes after step 4 and before the step-5 baton."
    - id: close
      keywords: [close task, finish task, mark done, keep folder]
      user_objective: "Mark an active task's work done while keeping the folder reopenable."
      operation: task.py close <ref> [--root PATH]
      details: references/lifecycle-operations.md#close
    - id: reopen
      keywords: [reopen task, undo close, back to active, resurrect]
      user_objective: "Reverse a terminal state back to active."
      operation: task.py reopen <ref> [--root PATH]
      details: references/lifecycle-operations.md#reopen
    - id: archive
      keywords: [archive task, finish for good, git is the record, closure policy, durable outputs, durable outputs check, document outlives the task, deleted spec]
      user_objective: "Retire an active task per its closure policy."
      operation: task.py archive <ref> [--root PATH]
      details: references/lifecycle-operations.md#archive
    - id: delete
      keywords: [delete task, remove folder, unconditional removal, discard]
      user_objective: "Archive semantics plus unconditional folder removal (even tmp)."
      operation: task.py delete <ref> [--root PATH]
      details: references/lifecycle-operations.md#delete
    - id: move
      keywords: [move task, promote, demote, relocate folder, rewrite references]
      user_objective: "Relocate a task between tmp and dev/tasks, keeping every reference valid."
      operation: task.py move <ref> <tmp|dev/tasks> [--root PATH]
      details: references/lifecycle-operations.md#move
    - id: list
      keywords: [list tasks, open tasks, enumerate, discovery, scope, filter]
      user_objective: "Enumerate tasks in a scope (folder crawl + task_list reference scan)."
      operation: task.py list [--scope all|user|project|skill|file] [--target X] [--status S] [--priority P] [--format text|json|yaml] [--root PATH]
      details: references/query-operations.md#list
    - id: triage
      keywords: [triage, triage the tasks, task triage, audit tasks, oldest first, review old tasks, /task triage]
      user_objective: "Audit open and deferred tasks, oldest first, and present one disposition decision at a time for the user."
      operation: "/task triage [<ref>] -- agent-side; no CLI verb"
      details: references/task-triage-protocol.md#task-triage-protocol
      steps:
        - n: 1
          action: "Load references/task-triage-protocol.md and follow its trigger, scope, selection, audit, presentation, and ruling procedures."
    - id: review
      keywords: [review tasks, task overview, task summaries, task answers, open tasks, closed tasks, missing summaries]
      user_objective: "Generate and open an HTML review of non-archived tasks in configured projects, or in the current project when no project directories are configured."
      operation: task.py review [--scope all|user|project|skill|file] [--target X] [--output PATH|-] [--no-open] [--generate-missing-summaries | --no-generate-missing-summaries] [--root PATH]
      details: references/task-review.md#review
    - id: answers
      keywords: [task answers, answers refresh, answers status, answers prune, orientation, coverage, question catalog]
      user_objective: "Compute, inspect or prune the answers of the question catalog (orientation, decisions, activity, task commits) for the tasks of configured projects."
      operation: task.py answers refresh|status|prune --format json --protocol N [--parent DIR ...] [--project DIR ...] [--allow CLASSES] [--batch N] [--stop-file PATH] [--deadline-s N]; task.py answers protocols --format json
      details: references/task-export-protocol.md#answers-commands
    - id: dashboard
      keywords: [task dashboard, dashboard, /task dashboard, open the dashboard, task-kit, browser view of tasks]
      user_objective: "Open the browser view over every configured project's task folders."
      operation: "/task dashboard -- agent-side routing; no task.py verb. Runs bare task-kit: bash \"${CLAUDE_PLUGIN_ROOT}/bin/task-kit\""
      details: references/dashboard.md#the-task-view
      steps:
        - n: 1
          action: "Run `bash \"${CLAUDE_PLUGIN_ROOT}/bin/task-kit\"`; report exit status and stdout, or failure stderr."
    - id: queue
      keywords: [queue, queued work, queue this, queue it, queue up, add to the queue, put in the queue, run unattended, do this when you can]
      user_objective: "Take a unit of work the user queues, make it runnable unattended, and hand it to a runner other than the main thread."
      operation: "queue <work> -- agent-side; no CLI verb. Hands the unit to a background worker per awesome-kit:orchestrate."
      details: references/background-workflows.md#queue
      steps:
        - n: 1
          action: "Clarify first in one batch. Do not queue an open question or execute queued work in the main thread."
    - id: show
      keywords: [show task, task details, read fields, inspect]
      user_objective: "Render one task's selected task.yaml fields, cheaply."
      operation: task.py show <ref> [--root PATH]
      details: references/query-operations.md#show
    - id: items
      keywords: [items, task items, work items, open items, goals, priorities, what next, what can I work on, what is available, sub-tasks, enumerate items, item menu]
      user_objective: "Enumerate a named task's open items -- the menu of next work within the task, with states."
      operation: task.py items <ref> [--state S] [--priority P] [--root PATH]
      details: references/query-operations.md#items
      gotchas:
        - "Edit plan.md's task_items block; no item CLI flags exist, and completion means removal."
    - id: status
      keywords: [task status, summarize task, where does it stand, background summary]
      user_objective: "Get a human summary of where a task stands (any task, any state)."
      operation: task.py status <ref> [--root PATH]
      details: references/background-workflows.md#status
      steps:
        - n: 1
          action: "Run status, then MUST dispatch a background summarizer through orchestrate. Do not read plan.md/log.md or summarize inline; lead with the parsed item menu."
    - id: sessions
      keywords: [who is working on, is anyone working on, working sessions, registered session, which session, release registration, unregister, stale registration, publish session records]
      user_objective: "See which Claude Code sessions are registered as working which tasks, release a registration, or publish this machine's pending records."
      operation: task.py sessions [list] [--scope project|all] [--format text|json] [--root PATH] | task.py sessions release <ref> [--all-local] [--root PATH] | task.py sessions publish [--root PATH]
      details: references/query-operations.md#sessions
  gotchas:
    - "Never infer task status from a task_list reference; resolve the folder through list, show, or validate."
```

## Reference index

Other members: `references/handoff-template.md`,
`references/example-claude-md.md`, `design/`,
`defaults/answers.yaml`, and
`questions/{activity,catalog,decisions,orientation,task_commits}.yaml`.
