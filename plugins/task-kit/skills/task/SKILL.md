---
_schema_version: 1
name: task
author: christina
skill-type: capability-skill
description: Use when creating, listing, working, queueing, closing, archiving or moving task folders and task_list refs. Do NOT use for TaskCreate/TodoWrite or code review.
---

# Task

Dispatch surface for the task system: one CLI (`scripts/task.py`) exposing 16
verbs over file-backed task folders. This skill routes a natural-language task
request ("start a task for X", "what tasks are open here?", "what can I work
on in this task?", "archive the closet task") to the right verb invocation
and tells you what the agent -- not the script -- must do with the output.
The system evolved from the `/hand-off` skill; the task folder IS the
generalized hand-off folder, and `hand-off` survives as the name of the v1
task TYPE (`type:` in task.yaml).

## Data model (one breath)

The task **folder** is the source of truth -- task-level state lives in its
`task.yaml`; the open **items** (the enumerable units of next work within the
task; synonym "work item") live in plan.md's embedded `task_items:` block,
and ONLY there (completion = removal; CLAUDE.md priorities reference items by
id, never restating state). **References** (`task_list:` blocks embedded in
any markdown doc) are inert pointers: `{path, host?}`, no status, resolved by
reading the folder. The task **id is its path** (`tmp/<stub>` or
`dev/tasks/<stub>`), so `move` must rewrite every reference. Every operation
names its task explicitly. **Durability is location**: `tmp/<stub>`
is ephemeral and machine-local; `dev/tasks/<stub>` is version-controlled --
archiving a dev/tasks folder records its final state in version control, then
deletes it, because version control is the record. Git is the VCS the scripts
detect and automate; in a workspace under another VCS (e.g. Perforce) the
scripts run no git commands and leave submission to you/the agent. Archiving
a tmp folder parks it at `tmp/archived-tasks/<stub>` (purge that directory
whenever you like); so does a dev/tasks folder when git ignores EVERY file
in it, at `dev/tasks/archived-tasks/<stub>` -- such a folder is local scratch
in fact, and version control holds no copy of a parked folder under either
root. A dev/tasks folder git holds only PARTLY (some files force-added, the
rest ignored) is kept in place instead: moving it would take tracked files
off their tracked paths with no commit.

## Queued work

Queue means: a unit of work to do when capacity allows. Four rules define it:

- **Unattended.** The unit completes end to end with no user involvement. A unit
  that needs a user decision, access or approval is not queueable yet.
- **Clarified first.** When the user queues work, ask whatever the unit needs to
  run unattended, before it is queued. Nothing with an open question enters the
  queue.
- **In parallel.** Queued work runs beside other work, or alone when there is
  none.
- **Never in the main thread.** The session that took the request does not do
  the work itself.

The `queue` capability below is the agent behavior; a background worker
launched per awesome-kit:orchestrate is the mechanism that runs it.

## Invoking the CLI

Run from the project root (refs are project-relative; `--root` defaults to
cwd) -- when `dev/tasks` is a link into another repository, the project root is
still the PROJECT, never the link target (see gotchas). Use the plugin venv's
Python explicitly -- not `uv run python`, which resolves the wrong environment
from a foreign cwd:

```bash
# macOS/Linux (Windows: .venv/Scripts/python.exe instead of .venv/bin/python)
"${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}"/plugins-kit/task-kit/.venv/bin/python \
  "${CLAUDE_SKILL_DIR}/scripts/task.py" <verb> [args]
```

Every `operation:` below abbreviates that invocation as `task.py`. Conventions:
exit 0 = success, non-zero = failure/block; findings and errors print to
stderr, results to stdout. `<ref>` is a path (`tmp/<stub>`, `dev/tasks/<stub>`)
or a bare stub (ambiguous stub = error listing candidates).

```yaml
capability_skill:
  _schema_version: "1"
  identity: "Dispatch surface routing natural-language task requests to the task-system CLI's 16 verbs and defining the agent-side behaviors (work's Skill lines, status's background summarizer, update's rotation) the scripts deliberately leave to the agent."
  scope:
    covers:
      - "Creating, listing, showing, validating, reviewing, and summarizing file-backed task folders (tmp/<stub>, dev/tasks/<stub>)"
      - "Enumerating a task's open items (plan.md's task_items unit) with their states -- the item-level menu below the task level"
      - "Lifecycle transitions: close, reopen, archive, delete, and move (with task_list reference rewriting)"
      - "Working-session registrations: which Claude Code session, on which machine, is working a task -- registered by work, released by the hand-off step, shown by list, show, status and the sessions verb"
      - "Opening the task view: /task dashboard runs bare `task-kit`, which opens databench's tasks view (references/dashboard.md)"
      - "Queued work: the definition (section Queued work) and the agent behavior when the user queues work (the queue capability)"
      - "Filling in scaffolded task folders per the hand-off template (references/handoff-template.md)"
    excludes:
      - "Native Claude Code task tracking (TaskCreate / TaskUpdate / TodoWrite) -- different system entirely"
      - "Perforce / git code review work (p4-kit:p4-code-review, git-kit:git-code-review)"
      - "Designing new task types or changing verb behavior (see design/task-system-design.md; code is authoritative)"
  external_capability:
    kind: tool
    name: task.py (task-system CLI)
    description: "Single-entry-point CLI at skills/task/scripts/task.py with 16 verb subcommands over task folders, task.yaml records, task_list references, plan.md task_items blocks, and working-session registrations. 15 verbs are script-driven; status is the one inference verb (the script prints substrate only). Review and answers are script-driven and may ask a model for task answers through llm-kit."
  layering:
    claude_md: []
    skill_md:
      - "The data model in one breath (folder = SoT, items in plan.md's task_items block, refs inert, id = path, explicit task refs, durability = location)"
      - "The canonical venv-python invocation and CLI conventions"
      - "The 16-verb capability surface with per-verb contracts"
      - "The agent-side behaviors the scripts leave open (work's initialization block -- invoke every emitted skill, then dispatch -- status background summary, update rotation, and the hand-off packaging plus baton, which run only when the user invokes /task hand-off)"
    references:
      - "handoff-template.md -- how to fill in and rotate a scaffolded folder's CLAUDE.md / plan.md / log.md"
      - "example-claude-md.md -- worked example of a fully-populated task-folder CLAUDE.md"
      - "the communication framework in the awesome-kit:verbose-updates skill (references/communication-framework.md) -- shared vocabulary (work-unit, auto-loaded vs on-demand, hand-off baton)"
      - "design/task-system-design.md -- the full design spec (entities, states, per-verb contracts, validation rules)"
  capabilities:
    - id: init
      keywords: [create task, new task, init, scaffold folder, start tracking]
      user_objective: "Create a new task folder with scaffolding, seeded from the current request."
      operation: task.py init <stub|desc> [--dest tmp|dev/tasks] [--type hand-off] [--root PATH]
      gotchas:
        - "Contract: output is always a valid active task -- on any validation failure init removes the folder and exits non-zero; it never leaves a partial/invalid folder. Prints the absolute folder path on success."
        - "Default dest is tmp (ephemeral). Existing target path errors -- use update, not init. A freeform description argument derives the stub; a path-shaped argument is sanitized, never treated as a path. Scope the stub to the work-unit (phase or project), never the session -- atoms-17-phase2, not atoms-22 (handoff-template.md)."
        - "After init, the folder holds placeholder scaffolds -- filling them in is agent work per references/handoff-template.md."
    - id: validate
      keywords: [validate task, check task, findings, classification, schema check]
      user_objective: "Check a task against its type schema and structural rules; classify it."
      operation: task.py validate <ref> [--root PATH]
      gotchas:
        - "Contract: prints the classification (active/blocked/closed/deferred/archived/invalid/remote) to stdout, errors AND warnings to stderr; exit 0 iff zero findings. All warnings originate here -- among them: uncommitted dev/tasks folder, dangling depends_on/blocked_by, orphaned tmp ref, no task_items block, stale item reference, document over its line ceiling."
        - "Findings gate work -- errors AND warnings both block. Fix forward (no recovery path), then re-validate."
        - "Advisory note: lines (NOT findings; exit code unaffected) also print to stderr -- approaching-budget, dominant-section, session-diary, version-control-unverified (outside any git repo; submit with the workspace VCS yourself), version-control-ignored (git will never carry the folder; relocate anything that must outlive it via update --durable-output). An `invalid answer file:` note (an unreadable file under the task's answers/ folder) is also advisory and never gates work; the answers engine rewrites its own invalid file on the next pass. Act on size notes at the next rotation; thresholds and remedies: handoff-template.md 'Document size budgets'."
    - id: work
      keywords: [work on task, start task, dispatch]
      user_objective: "Work an explicitly named task and load its working context."
      operation: task.py work <ref> [--init] [--root PATH]
      steps:
        - n: 1
          action: "Run the work verb. It validates first: ANY error OR warning blocks (exit non-zero, findings on stderr, initialization block not emitted). A remote ref (tmp + other host) cannot be worked locally. If no folder exists at the ref, work ERRORS -- treat that as a mistyped path (run list) rather than a task to create; --init opts into scaffolding the folder and working it, and is for a ref you meant to promote. Once the gate passes, inside a Claude Code session, the verb also REGISTERS this session as working the task and publishes the record: it commits this machine's file under the task root's .sessions/ directory on the checked-out branch, then fetches the upstream and fast-forwards or merges it into the checked-out HEAD and working tree (as a pull would; a merge that stops on conflicts is aborted), then pushes only when the net change from the upstream is session files. It never force-pushes, rebases, stashes or switches branches; `sessions: {record: false}` in ~/.claude/task.local.yaml turns it off. Registering adds `session:` lines on stderr and changes neither stdout nor the exit code. Relay any `session:` line to the user in one sentence; a `session: kept:` line names the release command for another session's registration, which you run only if the user asks."
        - n: 2
          action: "On success the script prints ONE initialization block to stdout: a '== task init ... ==' header, a Skill(skill: \"<name>\") line for every skill the task needs (the always-required baseline the script emits, followed by the task's own skills_to_invoke, deduped), an agent_hint: <type> line when set, and a closing '== then: start ... ==' directive. AGENT BEHAVIOR: invoke EVERY emitted Skill(...) line via the Skill tool now, in the order printed, before any other tool use -- the emitted list is the whole required set, so there is nothing to remember from elsewhere. Then act on the closing directive: START the work in this turn. Route planning and implementation through orchestrate's rendered policy, honoring the agent_hint type when eligible. The script only emits these lines; acting on them is your job."
      gotchas:
        - "A registration lasts from work until the hand_off capability's release step runs; exiting or crashing the harness does not end it, and it shows as `not running` on this machine and `unverified` elsewhere until released (`sessions release <ref> --all-local` is the explicit cleanup). `sessions: {record: false}` in ~/.claude/task.local.yaml turns registration off."
        - "Do not skip the emitted Skill(...) invocations -- they are the task's self-documented working context (the self-documenting-task pattern), not decoration."
        - "The emitted list is the SINGLE source of the required-skill set (baseline plus task-declared, merged script-side) -- do not treat the baseline as advisory. orchestrate is emitted unconditionally, even for trivial work. Once invoked through task, it has the same force as direct invocation: delegate every work-product implementation unit, including small code, tests, scripts, configuration, and documentation. Running task CLI verbs, read-only listings, or other existing scripts for coordination or verification is not by itself work-product implementation."
        - "work is a START, not a checkpoint. Say the orientation line the folder's opening response protocol asks for and then begin the first concrete action in the SAME turn. Do not end the turn on an unblocked task waiting for the user to say go. Two things still stop the turn and neither is waived: a real blocker, and a decision the folder's Autonomy status has claimed for the user."
    - id: update
      keywords: [update task, edit fields, refresh folder, set status, set priority, rotation]
      user_objective: "Upsert a task and apply task.yaml field edits; refresh the folder."
      operation: task.py update <ref> [--status S] [--priority P] [--description D] [--depends-on PATH ...] [--blocked-by PATH ...] [--agent-hint H] [--skill-to-invoke NAME ...] [--durable-output PATH ...] [--summary S] [--root PATH]
      gotchas:
        - "Contract: requires an explicit ref; inits when absent (upsert). Applies the field edits, appends one dated entry to log.md, re-validates, prints the classification; exit 0 iff no findings -- but the write persists regardless (fix-forward). List-valued flags are repeatable and REPLACE the stored list. --summary writes the legacy one-line task.summary; when the task's orientation answer exists, update prints a stderr note that the answer's headline supersedes it in list and review."
        - "--status deferred marks a task purposefully put on hold, intended to be resumed later -- it is neither open nor closed. A deferred task is excluded from list's Open tasks: section and review's Open group, sectioned in its own Deferred tasks: / Deferred group instead. `reopen <ref>` returns it to active."
        - "AGENT BEHAVIOR: the script's only document write is the dated log.md line -- it never rewrites plan.md. On every update that represents substantive progress, run the rotation passes yourself per handoff-template.md 'Rotation discipline' (rotation, stale-state, durable-outputs, vocabulary). Rotation is the standing discipline, not a cleanup that fires once a doc crosses its budget; the budgets are ENFORCED at the re-validate (over the ceiling = blocking warning, past the healthy target = advisory note; handoff-template.md 'Document size budgets')."
        - "DURABLE-OUTPUTS PASS: a folder document that would outlive the folder belongs in the repo it describes -- move it there NOW and declare it with --durable-output <repo-relative path> (repeatable; REPLACES the stored list). Judge at authoring time, not at archive, whose check is only a backstop. Declaring nothing is valid. Full rule: handoff-template.md 'Durable outputs'."
        - "update never packages the folder for a fresh session and never emits the hand-off baton. That is the hand_off capability below, and it is entered ONLY when the user invokes /task hand-off or /restart hand-off. An update that looks like the session's last, or a stopping point you judge natural, is not an invocation -- finish the turn normally."
        - "PRE-CONTRACT FOLDER (validate warns 'no task_items block'): the update pass STARTS with the one-time conversion in handoff-template.md 'Converting a pre-contract folder', run until validate is clean -- the block must end as the ONLY carrier of open-work state."
    - id: hand_off
      keywords: [hand-off, hand off, package for a fresh session, resume in a new session, baton, continue in a new session]
      user_objective: "Package the folder so a fresh session resumes with one paste -- ONLY when the user invokes /task hand-off or /restart hand-off."
      operation: "/task hand-off <ref> or /restart hand-off [<ref>] -- user-invoked; no CLI verb. Dispatches onto task.py update <ref> and task.py sessions release <ref> plus the agent-side packaging in the steps."
      steps:
        - n: 1
          action: "GATE: proceed only if the user's own message invoked /task hand-off <ref> or /restart hand-off [<ref>], or said in plain words to hand off / package the task for a new session. Nothing else qualifies: not an update that looks like the session's last, not a stopping point you judge natural, not the end of a long turn, and not the template's 'end the turn with the baton' (that instruction is scoped to THIS capability). Without the invocation there is no hand-off -- finish the turn normally and do not raise the idea yourself. When relaunch-kit:restart invoked this capability, its request_restart call comes after step 4 and before the step-5 baton."
        - n: 2
          action: "Run the update rotation (the update capability's AGENT BEHAVIOR, pre-contract conversion first if needed), then the update verb to record the dated log entry and re-validate. Findings block: fix forward until clean."
        - n: 3
          action: "Self-verify per handoff-template.md 'Self-verify': read CLAUDE.md + plan.md cold, as the next agent, and fix what a cold reader could not resolve."
        - n: 4
          action: "Release this session's registration: run `task.py sessions release <ref>` (the work verb registered it). Run it after the update findings are clean and the self-verify is done, because release is the last state change, and before the baton. FIRST, in a git task root, commit AND push the task folder (the update's log.md line and any self-verify fixes; task_records authorization), because the release publishes only when the net change from upstream is session files, so an unpushed folder commit makes it print `committed; not published: unpushed commits outside this machine's session records`. If the release still reports `not published`, push the task root (it carries both the folder commit and the release commit), then run `task.py sessions publish`. Relay its `session:` line to the user in one sentence; if it exits non-zero, relay the error and still end with the baton."
        - n: 5
          action: "End the turn with the hand-off baton -- exactly two lines, `CWD: <project root>` then `Continue: /task work <CWD-relative task-folder path>` (the awesome-kit:verbose-updates communication framework, 'hand-off baton'). The script emits neither line; both are your job, and they belong to this turn only."
      gotchas:
        - "Do not pass hand-off to task.py as a verb -- the CLI has no such verb, and hand-off is also the task TYPE name (--type flag). The dispatch is skill-level: the step-2 update call and the step-4 `sessions release` are the only script invocations."
    - id: close
      keywords: [close task, finish task, mark done, keep folder]
      user_objective: "Mark an active task's work done while keeping the folder reopenable."
      operation: task.py close <ref> [--root PATH]
      gotchas:
        - "Contract: requires an existing folder with stored status active. Sets status: closed, KEEPS the folder. Prints closed: <id>."
    - id: reopen
      keywords: [reopen task, undo close, back to active, resurrect]
      user_objective: "Reverse a terminal state back to active."
      operation: task.py reopen <ref> [--root PATH]
      gotchas:
        - "Contract: allowed only while the folder still exists -- including an archived folder parked at <location>/archived-tasks/<stub>, which reopen RESTORES to <location>/<stub> first. Both roots park: tmp always, dev/tasks when git ignores EVERY file in the folder. A task with no folder (and nothing parked) is gone -- it cannot be reopened. Sets status: active and re-validates (prints classification; exit reflects findings)."
        - "Unconditional means the prior stored status is never inspected -- closed, deferred, and a restored archived folder all reopen the same way, with no per-status branch."
    - id: archive
      keywords: [archive task, finish for good, git is the record, closure policy, durable outputs, durable outputs check, document outlives the task, deleted spec]
      user_objective: "Retire an active task per its closure policy."
      operation: task.py archive <ref> [--root PATH]
      gotchas:
        - "Contract: acts on an ACTIVE task (a closed or deferred task errors with a reopen-first hint). tmp -> status: archived, folder MOVED to tmp/archived-tasks/<stub> (the user-purgeable parking directory; an occupied spot refuses -- remove the old copy first). dev/tasks -> version control is the record: in a GIT repo the final state (status + dated log entry) is COMMITTED, the folder deleted, and the removal committed -- two commits scoped to the task folder, never removing the folder before its final state is committed; OUTSIDE a git repo no git command runs -- the final state is recorded and the folder KEPT ('vcs_pending'), and the AGENT submits it with the workspace's VCS (e.g. p4 submit), then runs delete."
        - "GIT-IGNORED task root (a project that deliberately keeps task folders as local scratch): git is present but will never carry the ignored files, so no commit is attempted and none is possible ('vcs_ignored'). Where git ignores EVERY file in the folder it IS local scratch, so it gets the tmp disposition -- the final state is recorded and the folder MOVED to dev/tasks/archived-tasks/<stub> (an occupied spot refuses, and the refusal lands before any write); the disposition says version control holds no copy to recover from, the parking directory is yours to purge, and delete removes the parked folder PERMANENTLY. Where git holds SOME of the folder and ignores the rest (files force-added into an ignored tree), the folder is KEPT IN PLACE and never parked -- moving it would take those tracked files off their tracked paths with no commit -- and the disposition names both sets, what git ignores and what git holds. This is a supported configuration, not an error; it is the one archive disposition where folder contents are unrecoverable, so relocate anything that must survive (update --durable-output) BEFORE running delete."
        - "DURABLE-OUTPUTS CHECK (runs BEFORE anything is parked, committed, or removed): each path in task.yaml's durable_outputs must exist AND live OUTSIDE the task folder. Either failure REFUSES the archive, naming every offender, while the documents can still be moved -- a path inside the folder is the load-bearing case, since archive is about to park or delete it. The check is purely mechanical and asks the user NOTHING; the judgment lives at authoring time (the update rotation's durable-outputs pass). No durable_outputs field at all -> a reminder note on stderr and the archive PROCEEDS: every folder predating the rule reads that way, and manifests here stay backwards-readable. On refusal: relocate each document to the repo it describes, re-declare with update --durable-output, then archive."
    - id: delete
      keywords: [delete task, remove folder, unconditional removal, discard]
      user_objective: "Archive semantics plus unconditional folder removal (even tmp)."
      operation: task.py delete <ref> [--root PATH]
      gotchas:
        - "Contract: acts on an ACTIVE or ARCHIVED task (a still-present archived folder -- archive's vcs_pending output after you submitted it, or a folder PARKED at <location>/archived-tasks/<stub> -- is exactly what delete finishes off; closed or deferred errors with a reopen-first hint). Name the task by its LIVE ref either way; delete finds the parked copy. Refuses a dev/tasks folder git can see is DIRTY (delete never auto-commits; use archive); outside a git repo, and for a parked folder, no git check applies -- there is no VCS state the script can verify or preserve, so the removal is unrecoverable. Then the folder is removed even when tmp. Prints deleted: <id>."
    - id: move
      keywords: [move task, promote, demote, relocate folder, rewrite references]
      user_objective: "Relocate a task between tmp and dev/tasks, keeping every reference valid."
      operation: task.py move <ref> <tmp|dev/tasks> [--root PATH]
      gotchas:
        - "Contract: id = path, so move REWRITES every task_list reference to the old path across all *.md under the project root (span-precise; prose mentions and other refs untouched). Prints moved: <old> -> <new> plus the rewritten-document count. The stub is preserved; refuses when the destination exists or the folder is absent/remote."
    - id: list
      keywords: [list tasks, open tasks, enumerate, discovery, scope, filter]
      user_objective: "Enumerate tasks in a scope (folder crawl + task_list reference scan)."
      operation: task.py list [--scope all|user|project|skill|file] [--target X] [--status S] [--priority P] [--format text|json|yaml] [--root PATH]
      gotchas:
        - "Contract: the default output has three sections, in order: Open tasks:, Deferred tasks:, and Closed tasks:. Open means active or blocked; Deferred means purposefully put on hold, intended to be resumed later (reopen returns it to active). Each task line is 'id  status  priority  last_update  title' (absent fields '-'); last_update is the latest ISO date in dated log.md entries. Explicit --status prints only matching task lines, including archived and other classifications. Dedupe by canonical path; classify each via validate. Exit 0 even when empty; notes and summary warnings go to stderr."
        - "--format text preserves the legacy line output; --format json or yaml emits the shared versioned listing projection, (version \"2\") including task.summary, summary_source (answer, legacy or null), summary_status, summary_missing, answers and coverage (the task answers, references/task-export-protocol.md), update history, and diagnostics for missing, stale or invalid summaries and answers. A task's current orientation answer headline replaces task.summary in summary. List never invokes inference or writes task folders."
        - "The project list is always computed (documents ARE the registry) -- there is no stored master list to consult or maintain."
        - "A task line with working-session registrations ends with ` [working: <host> <state> <age>; ...]` (state: running / not running on the machine that wrote the record, unverified on any other), and --format json|yaml carry `sessions` per task. List only reads them."
        - "--scope all scans direct, non-hidden project directories under each parent in ~/.claude/task.local.yaml's project_directories list. Entries are absolute paths or paths containing ${NAME} / %NAME% environment references; for example, project_directories: ['${DEVROOT}']. When the file is absent or declares no project_directories, the DEVROOT environment variable is the single parent. With neither, --scope all exits with an error naming both options. Each project contributes tmp/ and dev/tasks/; physical task folders reached through shared dev/tasks symlinks or junctions are emitted once, owned by the shortest project name."
        - "project/user scope enumerate the TASK ROOTS: folder crawl over tmp/ + dev/tasks/, plus a task_list reference scan of the *.md under those roots (the parked <root>/archived-tasks/ subtree excluded under either root). They do NOT crawl the whole tree -- an embedded task_list block is indistinguishable from an EXAMPLE of one, so a whole-tree scan reports the format's own documentation as live tasks. A task_list embedded elsewhere (a SKILL.md, a domain issues.md) is reached by NAMING its document: --scope skill <name> or --scope file <path>."
    - id: triage
      keywords: [triage, triage the tasks, task triage, audit tasks, oldest first, review old tasks, /task triage]
      user_objective: "Audit open and deferred tasks, oldest first, and present one disposition decision at a time for the user."
      operation: "/task triage [<ref>] -- agent-side; no CLI verb"
      steps:
        - n: 1
          action: "Load references/task-triage-protocol.md and follow its trigger, scope, selection, audit, presentation, and ruling procedures."
      gotchas:
        - "The disposition is the user's decision; do not apply archive, close, defer, update, merge, or reviewed-keep without a ruling."
        - "Present and resolve one task at a time, even when the audits were prepared in parallel."
        - "An empty scaffold is a direct never-started classification with an archive recommendation; it needs no audit."
    - id: review
      keywords: [review tasks, task overview, task summaries, task answers, open tasks, closed tasks, missing summaries]
      user_objective: "Generate and open an HTML review of non-archived tasks in configured projects, or in the current project when no project directories are configured."
      operation: task.py review [--scope all|user|project|skill|file] [--target X] [--output PATH|-] [--no-open] [--generate-missing-summaries | --no-generate-missing-summaries] [--root PATH]
      steps:
        - n: 1
          action: "Run the shared listing projection used by list. Default to all scope (project directories from ~/.claude/task.local.yaml, else DEVROOT) and group records by project after physical-folder deduplication; with neither available, exit with an error naming both options instead of narrowing to the current project. An explicit --scope project renders the current project's tasks as HTML. Group active/blocked tasks as Open, deferred tasks as Deferred (purposefully put on hold, intended to be resumed later), closed tasks as Closed, and omit archived tasks. Order projects by last update, newest first: a project's last update is the newest dated activity among its open tasks (closed tasks and summary dates do not count), and projects with none sort last by name. Within each group, sort tasks by each folder's latest log.md activity timestamp, newest first. Each task row dims with age: full brightness up to 4 hours old, fading linearly to 33% at 120 hours and beyond. Task rows do not expand and there is no Updates section; hovering over or keyboard-focusing a task name shows a card with its summary only."
        - n: 2
          action: "By default, answer the orientation question of every eligible local active, blocked, deferred, or closed task whose orientation answer is missing, stale or invalid, before rendering. The answers engine runs one pass per repository, asks the models of the answers chain (shipped default: luna, then haiku, at medium effort; the config key answers.models in ~/.claude/config/task-kit.yaml replaces the list) through llm-kit, tries free entries first, and uses a paid class only when llm-kit's per-machine consent allows it. Each answer is one file under the task's answers/ folder; review commits and publishes only this machine's answer files and writes neither task.yaml nor log.md. Use --no-generate-missing-summaries to inspect gaps without asking a model; --generate-missing-summaries is the explicit form of the default."
        - n: 3
          action: "Write a self-contained HTML document, open it in the user's browser unless --no-open is set, and report generation failures on stderr. --output - writes HTML to stdout; otherwise the default is a private temporary HTML file."
      gotchas:
        - "Missing or stale summaries have a distinct warning treatment in HTML and remain visible in the structured diagnostics. Unavailable, remote, invalid, or orphaned tasks are shown as unavailable and are never sent to the model. Answer states (current, stale, missing, invalid, not-applicable) have distinct treatments."
        - "Exit code: review exits 1 when a model refusal (llm-kit absent or too old; the message names the install or update command) or any failed answer was reported, and 0 otherwise; the HTML is written either way. Errors and advisory note: lines (uncommitted task folder skipped, answer superseded, answers waiting to be published) print to stderr."
        - "Answering writes no log entry, so it does not advance last_update. A substantive task change makes the answer stale (its stored fingerprint no longer matches the question and inputs) and the next default review answers it again. Model answering REFUSES when llm-kit is absent or older than 0.4.0; nothing substitutes, and every other verb works without llm-kit."
    - id: answers
      keywords: [task answers, answers refresh, answers status, answers prune, orientation, coverage, question catalog]
      user_objective: "Compute, inspect or prune the answers of the question catalog (orientation, decisions, activity, task commits) for the tasks of configured projects."
      operation: task.py answers refresh|status|prune --format json --protocol N [--parent DIR ...] [--project DIR ...] [--allow CLASSES] [--batch N] [--stop-file PATH] [--deadline-s N]; task.py answers protocols --format json
      gotchas:
        - "Contract: the same command as `task-kit answers`; it writes exactly one JSON document to stdout, diagnostics to stderr, and requires --format json and --protocol N (answers protocols takes only --format json). The payloads, flags and exit codes (0 ok, 1 internal/discovery_failed/config_invalid, 2 usage, 4 protocol_unsupported) are in references/task-export-protocol.md, section Answers commands."
        - "status writes nothing and takes no lock. refresh answers the pending questions of the most recently modified tasks (--batch, default 4 tasks per repository), commits only this machine's answer files and publishes them. prune deletes this machine's answer files of questions the catalog no longer holds."
        - "Spend: refresh asks free entries first. A usage or spend class entry runs only when --allow names the class or, without --allow, llm-kit's per-machine consent allows it. Do not pass --allow with a paid class unless the user said so."
        - "Model answering REFUSES when llm-kit is absent or too old: the repository report carries a refusal naming the install or update command. Relay it as printed; do not install llm-kit unasked."
    - id: dashboard
      keywords: [task dashboard, dashboard, /task dashboard, open the dashboard, task-kit, browser view of tasks]
      user_objective: "Open the browser view over every configured project's task folders."
      operation: "/task dashboard -- agent-side routing; no task.py verb. Runs bare task-kit: bash \"${CLAUDE_PLUGIN_ROOT}/bin/task-kit\""
      steps:
        - n: 1
          action: "Run `bash \"${CLAUDE_PLUGIN_ROOT}/bin/task-kit\"` (what typing `task-kit` in a terminal does once bootstrap has put it on PATH). It runs `databench --view tasks`, which serves databench's tasks view over task-kit's export. Report the command's exit status and its output, or its stderr when it fails."
      gotchas:
        - "task-kit no longer has a dashboard of its own. When databench is not on PATH, bare `task-kit` exits 1 and says databench provides the task view; relay that message as printed and do not install databench unasked. What databench shows and how it reads tasks: references/dashboard.md."
        - "`task-kit stop [--force]` stops a dashboard server an earlier task-kit version started, and refuses while that server has jobs running unless `--force`. Relay its message as printed; do not delete the server record or kill a process to get past a refusal."
    - id: queue
      keywords: [queue, queued work, queue this, queue it, queue up, add to the queue, put in the queue, run unattended, do this when you can]
      user_objective: "Take a unit of work the user queues, make it runnable unattended, and hand it to a runner other than the main thread."
      operation: "queue <work> -- agent-side; no CLI verb. Hands the unit to a background worker per awesome-kit:orchestrate."
      steps:
        - n: 1
          action: "Clarify. Apply the definition in section Queued work: ask the user every question the unit needs to complete with no user involvement (scope, inputs, access, the check that shows it is done), batched in one message. Ask only what the unit cannot run without. Do not queue until the answers are in."
        - n: 2
          action: "Make the unit a task item when it belongs to a dev/tasks task: add the plan.md task_items entry and its `### <id> -- <title>` section with the clarified brief (and `accept:` when a check exists), so the brief survives the session. The CLI has no queue verb."
        - n: 3
          action: "Launch a background worker with the clarified brief per awesome-kit:orchestrate (run_in_background), and keep the main thread free."
      gotchas:
        - "Never do the queued work in the main thread, and never `/task work` a task to do its queued item inline."
        - "Do not restate the definition elsewhere; it lives in section Queued work only. Do not add queue verbs, routes or states."
    - id: show
      keywords: [show task, task details, read fields, inspect]
      user_objective: "Render one task's selected task.yaml fields, cheaply."
      operation: task.py show <ref> [--root PATH]
      gotchas:
        - "Contract: pure field read, no inference. Non-zero with a reason when the ref is unresolvable or the folder is not locally readable (archived / orphaned / remote). Ends with one `working: <host> <state> (<checkout>, session <id>, since <time>)` line per working-session registration, or `working: -`."
    - id: items
      keywords: [items, task items, work items, open items, goals, priorities, what next, what can I work on, what is available, sub-tasks, enumerate items, item menu]
      user_objective: "Enumerate a named task's open items -- the menu of next work within the task, with states."
      operation: task.py items <ref> [--state S] [--priority P] [--root PATH]
      gotchas:
        - "Contract: requires an explicit ref and reads plan.md's task_items unit; one parseable line per item -- 'id  state  priority  title' (absent priority '-') -- sorted by priority then block order; --state/--priority filter (states: available | in-flight | blocked-user | deferred). Exit 0 even when empty; block findings go to stderr as notes (validate is the gate that reports them as findings)."
        - "An unresolvable ref or a missing local task directory exits non-zero with a reason (archived, orphaned, or remote refs may have no directory readable locally)."
        - "This answers 'what else can I work on in this task?' -- item-level, within one task. For the task-level question ('what tasks are open here?') use list. A pre-contract folder (no task_items block yet) prints a note pointing at the one-time conversion (handoff-template.md 'Converting a pre-contract folder')."
        - "AGENT BEHAVIOR: item edits are plan.md edits -- there are no item CLI flags. Add/remove/re-state items by editing the block directly during the update rotation, per references/handoff-template.md (completion = REMOVAL from the block; the block is the only place open work may live)."
    - id: status
      keywords: [task status, summarize task, where does it stand, background summary]
      user_objective: "Get a human summary of where a task stands (any task, any state)."
      operation: task.py status <ref> [--root PATH]
      steps:
        - n: 1
          action: "Run the status verb. The script prints the SUBSTRATE only: classification, findings, the task.yaml fields, the document paths (CLAUDE.md / plan.md / log.md), and the parsed task_items menu."
        - n: 2
          action: "AGENT BEHAVIOR: status is the system's ONE inference verb. Dispatch a BACKGROUND sub-agent (Task tool) to read the substrate's documents and produce the summary -- do NOT read plan.md/log.md and summarize inline in the main context. Lead the summary with the parsed open-item menu from the substrate, without re-parsing it. The point is main-context preservation; the sub-agent returns the short summary, you relay it. Route the dispatch through orchestrate and use whatever model its rendered routing policy resolves for this work -- do not pick or assume one here."
      gotchas:
        - "The substrate includes a `sessions:` block (or `sessions: none`) listing the task's working-session registrations; fold it into the summary when someone is working the task."
        - "Summarizing inline defeats the verb's reason for existing (context preservation). The script even prints a reminder note to this effect."
    - id: sessions
      keywords: [who is working on, is anyone working on, working sessions, registered session, which session, release registration, unregister, stale registration, publish session records]
      user_objective: "See which Claude Code sessions are registered as working which tasks, release a registration, or publish this machine's pending records."
      operation: task.py sessions [list] [--scope project|all] [--format text|json] [--root PATH] | task.py sessions release <ref> [--all-local] [--root PATH] | task.py sessions publish [--root PATH]
      gotchas:
        - "Contract: a registration is made by the work verb and ends only when it is released (the hand_off capability's step 4, or `release <ref> --all-local` by a person); harness exit and resume do not end it. The record lives in <task root>/.sessions/<host id>.yaml and is committed and pushed with the tasks' own repository, one file per machine."
        - "States: running and not running are judged only by the machine that wrote the record, from its exact process stamp; a record from any other machine is unverified. Every display shows the record's age."
        - "The default list and `list` are read-only and offline. `release <ref>` removes the calling session's registration of the task (it needs the Claude Code session; outside one, pass --all-local); `--all-local` removes every registration this machine holds for the task. Both publish; `publish` drains files committed but not yet pushed. A `not published` line means other machines cannot see the change until the next publish; when it names unpushed commits outside this machine's session records, commit and push the task root first."
        - "Release another session's registration only when the user asks; work keeps a not-running registration by another session and names the command."
  gotchas:
    - "hand-off names two things and neither is a CLI verb: the task TYPE (task.yaml type: field, --type flag; the skill was renamed hand-off -> task and the v1 type keeps the old name -- do not 'fix' type: hand-off to type: task) and the user-invoked packaging capability (/task hand-off <ref>, the hand_off record above)."
    - "Refs resolve against `--root`, which defaults to cwd -- so a `cd` into the task folder, or into the repository behind a `dev/tasks` link, breaks every ref-taking verb: a bare stub fails with 'matches no task folder under tmp/ or dev/tasks/', a path ref with 'not a known task location'. Run from the project root, or pass `--root <project>` when the shell must sit elsewhere."
    - "Use the explicit plugin-venv python shown under Invoking the CLI. The CLI self-repairs (re-execs under the provisioned venv via its vendored bootstrap_guard), but that path is the canonical, cwd-independent invocation."
    - "validate gates work with BOTH errors and warnings -- an uncommitted dev/tasks folder blocks work until committed, because durable work that exists only in the working tree is one rm away from gone. The check is git-scoped but the posture is VCS-neutral: outside a git repo, or where the folder is git-ignored, it is an advisory note (the scripts cannot check other VCS; a project keeping task folders as local scratch would otherwise have every task blocked). Same posture for the doc size budgets: over the ceiling blocks work until rotated (decompose per handoff-template.md, do not just trim)."
    - "References never carry status. To answer 'is X done?' resolve the folder (show/validate/list) -- do not infer from the presence or wording of a task_list entry."
    - "orchestrate owns planning placement, worker routing, optional delegation economics, and autonomy edges. Main-thread planning requires BOTH best-suited model fit under rendered policy AND sufficient verified, decision-relevant context; otherwise delegate planning. For optional coordination or read-only investigation, keep the unit on the main thread only when it is the best available model AND total delegation cost exceeds direct work. These choices never authorize main-thread work-product implementation: delegate that work even when one compact unit is cheaper than a split. `work` emits orchestrate as the first Skill(...) line -- invoke it from there. Name `model` explicitly per its tiers; a model-less dispatch skips the routing."
    - "Vocabulary: the unit below the task is an ITEM (long form 'task item'; accepted synonym 'work item') -- route all of 'work items', 'open items', 'goals', 'sub-tasks', 'what's available on this task' to the items verb. Never introduce a sub-task entity: an item has no folder, no lifecycle verbs, no outside references; an item that outgrows the block is promoted to a real task (init + a task_list ref)."
  anti_patterns:
    - id: inline_status_summary
      name: Summarizing status inline in the main context
      keywords: [inline summary, context bloat, foreground read, status verb]
      why_it_seems_right: "The substrate is already printed; reading plan.md and log.md and summarizing right here is one less dispatch."
      why_it_is_wrong: "status exists to preserve main context -- plan.md/log.md can be hundreds of lines each. Inline reading pays that token cost in the orchestrating context for a one-paragraph answer."
      alternative: "Dispatch a background sub-agent with the substrate paths; relay its short summary (the status capability's step 2)."
    - id: native_task_tools_for_folders
      name: Reaching for TaskCreate/TodoWrite for task-folder work
      keywords: [TaskCreate, TodoWrite, native tasks, wrong system]
      why_it_seems_right: "Claude Code has built-in task tools, and the request says 'task'."
      why_it_is_wrong: "Native tasks are session-scoped harness state; this system is durable on-disk folders with references, validation, and a lifecycle. They do not interoperate."
      alternative: "Route task-folder vocabulary (folders, task.yaml, task_list refs, tmp vs dev/tasks) here; use native task tools only for in-session step tracking."
    - id: unprompted_hand_off
      name: Handing off without being asked
      keywords: [unprompted baton, self-initiated hand-off, session boundary guess, end-of-turn baton, packaging without invocation, offering to hand off]
      why_it_seems_right: "The work reached a natural stopping point, this update looks like the session's last, and the template says the baton is required at the end of a hand-off turn -- emitting it now, or offering to, reads as diligence."
      why_it_is_wrong: "Whether a session ends is the USER's decision, and /task hand-off or /restart hand-off is how they make it. A baton the user did not ask for reads as the agent downing tools mid-task; a self-verify pass they did not ask for spends main context re-reading documents for a hand-off that is not happening. 'Required' in the template describes the hand-off turn, not every turn."
      alternative: "Finish the turn normally. Package and emit the baton only in the turn where the user invoked /task hand-off or /restart hand-off (the hand_off capability's gate)."
    - id: skipping_work_dispatch
      name: Treating work's output as informational
      keywords: [skill lines ignored, agent_hint ignored, work output, dispatch skipped]
      why_it_seems_right: "The command exited 0 and the initialization block was printed -- the verb succeeded, time to start coding. Or, in the partial form: the task's own skills WERE invoked, so initialization feels done."
      why_it_is_wrong: "The initialization block IS the task's working context -- the baseline skills, the skills the task declared it needs, the sub-agent type suited to it, and the closing dispatch directive. The observed failure is the PARTIAL one: invoke the task-declared skills, skip orchestrate, then implement the whole stage inline in the main context. Ignoring any part starts the work without what the task recorded for itself, and inline implementation spends the main context the dispatch exists to protect."
      alternative: "Invoke every emitted Skill(...) line via the Skill tool in the order printed, then obey the closing directive: dispatch per orchestrate to the agent_hint sub-agent type rather than implementing inline (the work capability's step 2)."
```

```yaml
references:
  - id: task_triage_protocol
    path: references/task-triage-protocol.md
    keywords: [triage, triage the tasks, task triage, audit tasks, oldest first, review old tasks, /task triage, task disposition, defer task]
    summary: "Protocol for selecting every open and deferred task (presented oldest first), running parallel read-only evidence audits, presenting one recommendation at a time within a 200-word summary, and applying the user's archive, close, defer, update, merge, or reviewed-keep ruling."
  - id: handoff_template
    path: references/handoff-template.md
    keywords: [hand-off template, seven sections, CLAUDE.md template, plan rotation, log filter, fill in scaffold, task_items block, item states, promotion rule, priorities reference items, pre-contract conversion, convert old folder, no task_items block, opening response protocol, orientation line, autonomy status, document size budgets, line budget, oversized document, approaching budget, dominant section, session diary, 400 lines, log.md exempt, durable outputs, document outlives the task, where does this doc live, spec deleted by archive, extraction at authoring time]
    summary: "How to fill in and maintain a hand-off-type task folder: the seven-section CLAUDE.md contract (Immediate Priorities = references to item ids), plan.md's task_items block + rotation discipline (completion = removal; promotion rule), the enforced document size budgets (note at the healthy target, blocking warning at the ceiling; log.md exempt), the one-time pre-contract-folder conversion procedure, log.md filter, the durable-outputs rule (does this document belong in the folder at all -- declare at authoring time, archive verifies), anti-patterns, self-verify. Load when populating or updating a scaffolded folder, when validate emits a size finding, or when archive refuses on durable outputs."
  - id: dashboard_reference
    path: references/dashboard.md
    keywords: [task dashboard, dashboard, databench, task view, task-kit export, task-kit stop, legacy dashboard, task-export, databench export, export contract, task-export.schema.json]
    summary: "Where the task view lives now: databench's tasks view over task-kit's export, what bare `task-kit` runs, and `task-kit stop` for a server an earlier version started. Load when the dashboard capability is routed and the question goes beyond launching it."
  - id: example_claude_md
    path: references/example-claude-md.md
    keywords: [worked example, produced CLAUDE.md, template made concrete]
    summary: "A fully-populated example of the task-folder CLAUDE.md the hand-off template prescribes. Load when the template feels abstract."
  - id: design_spec
    path: design/task-system-design.md
    keywords: [design spec, entities, states, verb contracts, validation rules, discovery algorithm]
    summary: "The full task-system design specification (implementation contract; code is authoritative on divergence). Load for semantics questions the capability records do not settle."
  - id: task_items_design
    path: design/task-items-design.md
    keywords: [task items design, item vocabulary, work item, item states, no done state, no sub-task, priorities as references, test case, converted task folder, evidence relocated]
    summary: "The task-items design proposal (ratified 2026-07-09): the vocabulary decision (item, not sub-task/goal/deliverable) and the full contract rationale; the mined evidence and the worked conversion are recorded outside this document. Load for why-questions about items; the operating contract lives in handoff-template.md."
```
