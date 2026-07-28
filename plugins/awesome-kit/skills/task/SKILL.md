---
_schema_version: 1
name: task
author: christina
skill-type: capability-skill
description: Use when creating, listing, working, closing, archiving, or moving task folders and task_list refs. Do NOT use for native TaskCreate/TodoWrite or code review.
---

# Task

Dispatch surface for the task system: one CLI (`scripts/task.py`) exposing 15
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
`dev/tasks/<stub>`), so `move` must rewrite every reference. There is **one
global `current` pointer** (a file at
`~/.claude/plugins/data/plugins-kit/awesome-kit/current`)
naming the single task being worked. **Durability is location**: `tmp/<stub>`
is ephemeral and machine-local; `dev/tasks/<stub>` is version-controlled --
archiving a dev/tasks folder records its final state in version control, then
deletes it, because version control is the record. Git is the VCS the scripts
detect and automate; in a workspace under another VCS (e.g. Perforce) the
scripts run no git commands and leave submission to you/the agent. Archiving
a tmp folder parks it at `tmp/archived-tasks/<stub>` (purge that directory
whenever you like).

## Invoking the CLI

Run from the project root (refs are project-relative; `--root` defaults to
cwd). Use the plugin venv's Python explicitly -- not `uv run python`, which
resolves the wrong environment from a foreign cwd:

```bash
# macOS/Linux (Windows: .venv/Scripts/python.exe instead of .venv/bin/python)
~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python \
  "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py" <verb> [args]
```

Conventions: exit 0 = success, non-zero = failure/block; findings and errors
print to stderr, results to stdout. `<ref>` is a path (`tmp/<stub>`,
`dev/tasks/<stub>`) or a bare stub (ambiguous stub = error listing
candidates).

```yaml
capability_skill:
  _schema_version: "1"
  identity: "Dispatch surface routing natural-language task requests to the task-system CLI's 15 verbs and defining the agent-side behaviors (work's Skill lines, status's background summarizer, update's rotation) the scripts deliberately leave to the agent."
  scope:
    covers:
      - "Creating, listing, showing, validating, and summarizing file-backed task folders (tmp/<stub>, dev/tasks/<stub>)"
      - "Enumerating a task's open items (plan.md's task_items unit) with their states -- the item-level menu below the task level"
      - "Working/switching the single global current task and acting on the emitted Skill(...) / agent_hint lines"
      - "Lifecycle transitions: close, reopen, archive, delete, and move (with task_list reference rewriting)"
      - "Filling in scaffolded task folders per the hand-off template (references/handoff-template.md)"
    excludes:
      - "Native Claude Code task tracking (TaskCreate / TaskUpdate / TodoWrite) -- different system entirely"
      - "Perforce / git code review work (p4-kit:p4-code-review, git-kit:git-code-review)"
      - "Designing new task types or changing verb behavior (see design/task-system-design.md; code is authoritative)"
  external_capability:
    kind: tool
    name: task.py (task-system CLI)
    description: "Single-entry-point CLI at skills/task/scripts/task.py with 15 verb subcommands over task folders, task.yaml records, task_list references, plan.md task_items blocks, and the global current pointer. 14 verbs are script-driven; status is the one inference verb (the script prints substrate only)."
  layering:
    claude_md: []
    skill_md:
      - "The data model in one breath (folder = SoT, items in plan.md's task_items block, refs inert, id = path, one current pointer, durability = location)"
      - "The canonical venv-python invocation and CLI conventions"
      - "The 15-verb capability surface with per-verb contracts"
      - "The agent-side behaviors the scripts leave open (work dispatch, status background summary, update rotation, and the required end-of-turn hand-off baton the update/hand-off packaging must emit)"
    references:
      - "handoff-template.md -- how to fill in and rotate a scaffolded folder's CLAUDE.md / plan.md / log.md"
      - "example-claude-md.md -- worked example of a fully-populated task-folder CLAUDE.md"
      - "communication-framework.md -- shared vocabulary (work-unit, auto-loaded vs on-demand, hand-off baton)"
      - "design/task-system-design.md -- the full design spec (entities, states, per-verb contracts, validation rules)"
  capabilities:
    - id: init
      keywords: [create task, new task, init, scaffold folder, start tracking]
      user_objective: "Create a new task folder with scaffolding, seeded from the current request."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        init <stub|desc> [--dest tmp|dev/tasks] [--type hand-off] [--root PATH]
      gotchas:
        - "Contract: output is always a valid active task -- on any validation failure init removes the folder and exits non-zero; it never leaves a partial/invalid folder. Prints the absolute folder path on success."
        - "Default dest is tmp (ephemeral). Existing target path errors -- use update, not init. A freeform description argument derives the stub; a path-shaped argument is sanitized, never treated as a path. Scope the stub to the work-unit (phase or project), never the session -- atoms-17-phase2, not atoms-22 (handoff-template.md)."
        - "After init, the folder holds placeholder scaffolds -- filling them in is agent work per references/handoff-template.md."
    - id: validate
      keywords: [validate task, check task, findings, classification, schema check]
      user_objective: "Check a task against its type schema and structural rules; classify it."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        validate <ref> [--root PATH]
      gotchas:
        - "Contract: prints the classification (active/blocked/closed/archived/invalid/remote) to stdout, errors AND warnings to stderr; exit 0 iff zero findings. All warnings originate here (non-tmp archived folder, uncommitted dev/tasks folder, dangling depends_on/blocked_by, orphaned tmp ref, no task_items block, stale item reference, oversized document -- a doc over its line ceiling: CLAUDE.md/plan.md 400, other docs 800, log.md exempt)."
        - "Findings gate work -- errors AND warnings both block. Fix forward (no recovery path), then re-validate."
        - "Advisory note: lines (NOT findings; exit code unaffected) also print to stderr: approaching-budget (doc past its healthy target -- CLAUDE.md 250, plan.md 300), dominant-section (one ## section over half of CLAUDE.md/plan.md), session-diary (dated narrative accreting in CLAUDE.md), version-control-unverified (a dev/tasks folder outside any git repo -- the scripts cannot check other VCS; ensure it is submitted, e.g. via p4). Act on the size notes at the next rotation; thresholds and remedies in references/handoff-template.md 'Document size budgets'."
    - id: work
      keywords: [work on task, set current, activate task, start task, dispatch]
      user_objective: "Make a task the single global current task and load its working context."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        work <ref> [--root PATH] [--pointer PATH]
      steps:
        - n: 1
          action: "Run the work verb. It validates first: ANY error OR warning blocks (exit non-zero, findings on stderr, pointer unwritten). A remote ref (tmp + other host) cannot be worked locally. If no folder exists at the ref, work auto-inits it (promotion)."
        - n: 2
          action: "On success the script writes the current pointer and prints one Skill(skill: \"<name>\") line per skills_to_invoke entry plus an agent_hint: <type> line when set. AGENT BEHAVIOR: actually invoke each emitted skill via the Skill tool now; then, for a non-trivial task, invoke the orchestrate skill (Skill: awesome-kit:orchestrate) and dispatch the work to background agents per it, honoring the agent_hint type when set. The script only emits these lines; acting on them is your job."
      gotchas:
        - "Do not skip the emitted Skill(...) invocations -- they are the task's self-documented working context (the self-documenting-task pattern), not decoration."
    - id: switch
      keywords: [switch task, change current, swap task, update then work]
      user_objective: "Update the currently-worked task, then make another task current."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        switch <ref> [--root PATH] [--pointer PATH]
      gotchas:
        - "Contract: runs update on the current task when one is set and extant (a note: line on stderr reports it; it becomes a plain active task -- no lingering claim), then behaves exactly like work <ref> (same gate, same emitted Skill/agent_hint lines -- act on them). If nothing is current, identical to work."
    - id: update
      keywords: [update task, edit fields, refresh folder, set status, set priority, rotation]
      user_objective: "Upsert a task and apply task.yaml field edits; refresh the folder."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        update [<ref>] [--status S] [--priority P] [--description D]
        [--depends-on PATH ...] [--blocked-by PATH ...] [--agent-hint H]
        [--skill-to-invoke NAME ...] [--durable-output PATH ...]
        [--root PATH] [--pointer PATH]
      gotchas:
        - "Contract: ref defaults to the current task; inits when absent (upsert). Applies the field edits, appends one dated entry to log.md, re-validates, prints the classification; exit 0 iff no findings -- but the write persists regardless (fix-forward). List-valued flags are repeatable and REPLACE the stored list."
        - "AGENT BEHAVIOR: the script's only document write is the dated log.md line -- it never rewrites plan.md. The real rotation discipline (completed-step detail plan -> log, REMOVING done items from the task_items block, promoting stray open items into it, stale-state pass, vocabulary pass, the document size budgets) is YOUR work, per references/handoff-template.md, whenever the update represents substantive session progress. Rotation is the standing discipline, applied by default on every such update -- not a cleanup that fires only once a doc crosses its budget. The budgets are ENFORCED at the re-validate: a doc over its ceiling (CLAUDE.md/plan.md 400 lines, other docs 800; log.md exempt) is a warning that makes update exit non-zero and blocks work; past the healthy target (CLAUDE.md 250, plan.md 300) it is an advisory note. Both name the largest sections to rotate (handoff-template.md 'Document size budgets')."
        - "DURABLE-OUTPUTS PASS (agent work, part of the rotation): for every non-scaffold doc in the folder (beyond CLAUDE.md/plan.md/log.md) apply the one-breath test -- 'if this folder were deleted today, would someone still need this document? If yes, its home is the repo it describes; the folder references it, never owns it.' Move such a document to its home in the OWNING repo now and declare it with --durable-output <repo-relative path> (repeatable; REPLACES the stored list). Do this at AUTHORING time, not at archive: a spec that only reaches its durable home when the task ends was undiscoverable for the whole life of the task. archive's check is a backstop, not the mechanism. Declaring nothing is a valid result -- if the content is already absorbed into as-built docs elsewhere, relocating it would create a second source of truth. Full rule: references/handoff-template.md 'Durable outputs'."
        - "HAND-OFF BATON (required end-of-turn output): when the update PACKAGES the folder for a fresh session -- an explicit hand-off, or any session-boundary update -- FIRST run the self-verify read (handoff-template.md 'Self-verify'): read CLAUDE.md + plan.md cold, as the next agent, and confirm the goal is legible without other docs, the concrete next step is known, the opening response is known, cwd + non-obvious operational rules are stated, nothing references conversation context a cold reader cannot resolve, and Project vocabulary decodes any renamed paths. THEN end the turn with the hand-off baton: a two-line block `CWD: <project root>` then `Continue: /task work <CWD-relative task-folder path>` (communication-framework.md 'hand-off baton'). Both are REQUIRED, not optional; the script emits neither, so both are the AGENT's job. Skipping the baton is the top hand-off miss -- it is what lets the next session resume with one paste."
        - "PRE-CONTRACT FOLDER (validate warns 'no task_items block'): the update/hand-off pass STARTS with the one-time conversion -- sweep all three docs for open work, dedupe into items, map triage states, rewrite Immediate Priorities as id references, retire the superseded GOAL/checkbox/banner forms, validate until clean. The full procedure is handoff-template.md 'Converting a pre-contract folder'; the block must end as the ONLY carrier of open-work state (a half-conversion recreates the drift)."
    - id: close
      keywords: [close task, finish task, mark done, keep folder]
      user_objective: "Mark an active task's work done while keeping the folder reopenable."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        close <ref> [--root PATH] [--pointer PATH]
      gotchas:
        - "Contract: requires an existing folder with stored status active. Sets status: closed, KEEPS the folder, clears the current pointer if it names this task. Prints closed: <id>."
    - id: reopen
      keywords: [reopen task, undo close, back to active, resurrect]
      user_objective: "Reverse a terminal state back to active."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        reopen <ref> [--root PATH] [--pointer PATH]
      gotchas:
        - "Contract: allowed only while the folder still exists -- including a tmp archived folder parked at tmp/archived-tasks/<stub>, which reopen RESTORES to tmp/<stub> first. A task with no folder (and nothing parked) is gone -- it cannot be reopened. Sets status: active and re-validates (prints classification; exit reflects findings)."
    - id: archive
      keywords: [archive task, finish for good, git is the record, closure policy, durable outputs, durable outputs check, document outlives the task, deleted spec]
      user_objective: "Retire an active task per its closure policy."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        archive <ref> [--root PATH] [--pointer PATH]
      gotchas:
        - "Contract: acts on an ACTIVE task (a closed task errors with a reopen-first hint). tmp -> status: archived, folder MOVED to tmp/archived-tasks/<stub> (the user-purgeable parking directory; an occupied spot refuses -- remove the old copy first). dev/tasks -> version control is the record: in a GIT repo the final state (status + dated log entry) is COMMITTED, the folder deleted, and the removal committed -- two commits scoped to the task folder, never removing the folder before its final state is committed; OUTSIDE a git repo no git command runs -- the final state is recorded and the folder KEPT ('vcs_pending'), and the AGENT submits it with the workspace's VCS (e.g. p4 submit), then runs delete. Clears the pointer if current."
        - "DURABLE-OUTPUTS CHECK (runs BEFORE anything is parked, committed, or removed): each path in task.yaml's durable_outputs must exist AND live OUTSIDE the task folder. Either failure REFUSES the archive, naming every offender, while the documents can still be moved -- a path inside the folder is the load-bearing case, since archive is about to park or delete it. The check is purely mechanical and asks the user NOTHING; the judgment lives at authoring time (the update rotation's durable-outputs pass). No durable_outputs field at all -> a reminder note on stderr and the archive PROCEEDS: every folder predating the rule reads that way, and manifests here stay backwards-readable. On refusal: relocate each document to the repo it describes, re-declare with update --durable-output, then archive."
    - id: delete
      keywords: [delete task, remove folder, unconditional removal, discard]
      user_objective: "Archive semantics plus unconditional folder removal (even tmp)."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        delete <ref> [--root PATH] [--pointer PATH]
      gotchas:
        - "Contract: acts on an ACTIVE or ARCHIVED task (a still-present archived folder -- e.g. archive's vcs_pending output after you submitted it -- is exactly what delete finishes off; closed errors with a reopen-first hint). Refuses a dev/tasks folder git can see is DIRTY (delete never auto-commits; use archive); outside a git repo no git check applies -- the agent owns VCS state. Then the folder is removed even when tmp. Prints deleted: <id>; clears the pointer if current."
    - id: move
      keywords: [move task, promote, demote, relocate folder, rewrite references]
      user_objective: "Relocate a task between tmp and dev/tasks, keeping every reference valid."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        move <ref> <tmp|dev/tasks> [--root PATH] [--pointer PATH]
      gotchas:
        - "Contract: id = path, so move REWRITES every task_list reference to the old path across all *.md under the project root (span-precise; prose mentions and other refs untouched) and updates the pointer if it named the old path. Prints moved: <old> -> <new> plus the rewritten-document count. The stub is preserved; refuses when the destination exists or the folder is absent/remote."
    - id: current
      keywords: [current task, what am i working on, pointer, global current]
      user_objective: "Report the single global current task."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        current [--pointer PATH]
      gotchas:
        - "Contract: prints '<id>  <classification>  <title>' or 'none'. The pointer stores an absolute path and is self-resolving from any cwd; a stale pointer (missing folder) is cleared and reported as none -- no error."
    - id: list
      keywords: [list tasks, open tasks, enumerate, discovery, scope, filter]
      user_objective: "Enumerate tasks in a scope (folder crawl + task_list reference scan)."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        list [--scope user|project|skill|file] [--target X]
        [--status S] [--priority P] [--root PATH]
      gotchas:
        - "Contract: one parseable line per task -- 'id  status  priority  title' (absent fields '-'); dedupes by canonical path; classifies each via validate. Remote tasks list as '<path> @<host>  remote  -  -' (not locally resolvable). Folderless non-tmp refs read as archived; folderless local tmp refs as orphaned. Exit 0 even when empty; notes go to stderr."
        - "The project list is always computed (documents ARE the registry) -- there is no stored master list to consult or maintain."
        - "project/user scope enumerate the TASK ROOTS: folder crawl over tmp/ + dev/tasks/, plus a task_list reference scan of the *.md under those roots (the parked tmp/archived-tasks/ subtree excluded). They do NOT crawl the whole tree -- an embedded task_list block is indistinguishable from an EXAMPLE of one, so a whole-tree scan reports the format's own documentation as live tasks. A task_list embedded elsewhere (a SKILL.md, a domain issues.md) is reached by NAMING its document: --scope skill <name> or --scope file <path>."
    - id: show
      keywords: [show task, task details, read fields, inspect]
      user_objective: "Render one task's selected task.yaml fields, cheaply."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        show <ref> [--root PATH]
      gotchas:
        - "Contract: pure field read, no inference. Non-zero with a reason when the ref is unresolvable or the folder is not locally readable (archived / orphaned / remote)."
    - id: items
      keywords: [items, task items, work items, open items, goals, priorities, what next, what can I work on, what is available, sub-tasks, enumerate items, item menu]
      user_objective: "Enumerate the current (or a named) task's open items -- the menu of next work within the task, with states."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        items [<ref>] [--state S] [--priority P] [--root PATH]
      gotchas:
        - "Contract: reads plan.md's task_items unit; one parseable line per item -- 'id  state  priority  title' (absent priority '-') -- sorted by priority then block order; --state/--priority filter (states: available | in-flight | blocked-user | deferred). Ref DEFAULTS TO THE CURRENT TASK. Exit 0 even when empty; block findings go to stderr as notes (validate is the gate that reports them as findings)."
        - "This answers 'what else can I work on in this task?' -- item-level, within one task. For the task-level question ('what tasks are open here?') use list. A pre-contract folder (no task_items block yet) prints a note: run the one-time conversion per handoff-template.md 'Converting a pre-contract folder' (sweep docs, dedupe, triage states, priorities as references, retire old forms, validate clean)."
        - "AGENT BEHAVIOR: item edits are plan.md edits -- there are no item CLI flags. Add/remove/re-state items by editing the block directly during the update rotation, per references/handoff-template.md (completion = REMOVAL from the block; the block is the only place open work may live)."
    - id: status
      keywords: [task status, summarize task, where does it stand, background summary]
      user_objective: "Get a human summary of where a task stands (any task, any state)."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        status <ref> [--root PATH]
      steps:
        - n: 1
          action: "Run the status verb. The script prints the SUBSTRATE only: classification, findings, the task.yaml fields, the document paths (CLAUDE.md / plan.md / log.md), and the parsed task_items menu."
        - n: 2
          action: "AGENT BEHAVIOR: status is the system's ONE inference verb. Dispatch a BACKGROUND sub-agent (Task tool) to read the substrate's documents and produce the summary -- do NOT read plan.md/log.md and summarize inline in the main context. The point is main-context preservation; the sub-agent returns the short summary, you relay it. Summarization is workhorse-tier work per the orchestrate skill's model_selection (currently sonnet)."
      gotchas:
        - "Summarizing inline defeats the verb's reason for existing (context preservation). The script even prints a reminder note to this effect."
  gotchas:
    - "hand-off is the task TYPE name (task.yaml type: field, --type flag), not the skill name -- the skill was renamed hand-off -> task; the v1 type keeps the old name. Do not 'fix' type: hand-off to type: task."
    - "Use the explicit plugin-venv python shown above. The CLI self-repairs (it re-execs under the provisioned venv via its vendored bootstrap_guard), but the venv path is the canonical, cwd-independent invocation."
    - "The current pointer is USER-GLOBAL (one across all projects), matching 'one active task globally'. Tests and sandboxes must inject --pointer; never point real work at a scratch pointer or vice versa."
    - "validate gates work with BOTH errors and warnings -- a dev/tasks folder git can see is uncommitted (a warning) blocks work until committed. This is deliberate: durable work that exists only in the working tree is one rm away from gone. The check is git-scoped but the posture is VCS-neutral: outside a git repo it is an advisory note, not a warning (the workspace may use Perforce or another VCS the scripts cannot check -- keeping the folder submitted is then the agent's job). Same posture for the doc size budgets: a CLAUDE.md or plan.md over its 400-line ceiling blocks work until rotated (decompose per handoff-template.md -- history to log.md, detail to referenced docs -- do not just trim)."
    - "References never carry status. To answer 'is X done?' resolve the folder (show/validate/list) -- do not infer from the presence or wording of a task_list entry."
    - "REQUIRED: when working on a non-trivial task, invoke the orchestrate skill (Skill: awesome-kit:orchestrate). It owns the delegation economics (what goes to a background agent vs inline) and the model-tier selection for every agent dispatch; this skill does not restate them. Name `model` explicitly per its tiers -- a model-less dispatch skips the routing."
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
    - id: skipping_work_dispatch
      name: Treating work's output as informational
      keywords: [skill lines ignored, agent_hint ignored, work output, dispatch skipped]
      why_it_seems_right: "The pointer is set and the command exited 0 -- the verb succeeded, time to start coding."
      why_it_is_wrong: "The Skill(...) and agent_hint: lines ARE the task's working context -- skills the task declared it needs and the sub-agent type suited to it. Ignoring them starts the work without the vocabulary the task recorded for itself."
      alternative: "Invoke each emitted skill via the Skill tool, then weigh dispatching to the agent_hint sub-agent type before proceeding (the work capability's step 2)."
```

```yaml
references:
  - id: handoff_template
    path: references/handoff-template.md
    keywords: [hand-off template, eight sections, CLAUDE.md template, plan rotation, log filter, fill in scaffold, task_items block, item states, promotion rule, priorities reference items, pre-contract conversion, convert old folder, no task_items block, document size budgets, line budget, oversized document, approaching budget, dominant section, session diary, 400 lines, log.md exempt, durable outputs, document outlives the task, where does this doc live, spec deleted by archive, extraction at authoring time]
    summary: "How to fill in and maintain a hand-off-type task folder: the eight-section CLAUDE.md contract (Immediate Priorities = references to item ids), plan.md's task_items block + rotation discipline (completion = removal; promotion rule), the enforced document size budgets (note at the healthy target, blocking warning at the ceiling; log.md exempt), the one-time pre-contract-folder conversion procedure, log.md filter, the durable-outputs rule (does this document belong in the folder at all -- declare at authoring time, archive verifies), anti-patterns, self-verify. Load when populating or updating a scaffolded folder, when validate emits a size finding, or when archive refuses on durable outputs."
  - id: example_claude_md
    path: references/example-claude-md.md
    keywords: [worked example, produced CLAUDE.md, template made concrete]
    summary: "A fully-populated example of the task-folder CLAUDE.md the hand-off template prescribes. Load when the template feels abstract."
  - id: communication_framework
    path: references/communication-framework.md
    keywords: [glossary, work-unit, auto-loaded vs on-demand, hand-off baton, provenance triad]
    summary: "Canonical shared vocabulary for cross-turn and cross-session communication; cited by this skill and verbose-updates."
  - id: design_spec
    path: design/task-system-design.md
    keywords: [design spec, entities, states, verb contracts, validation rules, discovery algorithm]
    summary: "The full task-system design specification (implementation contract; code is authoritative on divergence). Load for semantics questions the capability records do not settle."
  - id: task_items_design
    path: design/task-items-design.md
    keywords: [task items design, item vocabulary, work item, item states, no done state, no sub-task, priorities as references, mined evidence, homeassistant test case]
    summary: "The task-items design proposal (ratified 2026-07-09): evidence from real task folders, the vocabulary decision (item, not sub-task/goal/deliverable), and the full contract rationale. Load for why-questions about items; the operating contract lives in handoff-template.md."
```
