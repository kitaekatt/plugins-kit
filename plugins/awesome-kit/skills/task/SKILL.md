---
_schema_version: 1
name: task
author: christina
skill-type: capability-skill
description: Use when creating, listing, working, closing, archiving, or moving task folders and task_list refs. Do NOT use for native TaskCreate/TodoWrite or code review.
---

# Task

Dispatch surface for the task system: one CLI (`scripts/task.py`) exposing 14
verbs over file-backed task folders. This skill routes a natural-language task
request ("start a task for X", "what tasks are open here?", "archive the
closet task") to the right verb invocation and tells you what the agent --
not the script -- must do with the output. The system evolved from the
`/hand-off` skill; the task folder IS the generalized hand-off folder, and
`hand-off` survives as the name of the v1 task TYPE (`type:` in task.yaml).

## Data model (one breath)

The task **folder** is the source of truth -- all structured state lives in
its `task.yaml`. **References** (`task_list:` blocks embedded in any markdown
doc) are inert pointers: `{path, host?}`, no status, resolved by reading the
folder. The task **id is its path** (`tmp/<stub>` or `dev/tasks/<stub>`), so
`move` must rewrite every reference. There is **one global `current`
pointer** (a file at `~/.claude/plugins/data/plugins-kit/awesome-kit/current`)
naming the single task being worked. **Durability is location**: `tmp/<stub>`
is ephemeral and machine-local; `dev/tasks/<stub>` is git-tracked -- archiving
a dev/tasks folder deletes it because git is the record.

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
  identity: "Dispatch surface routing natural-language task requests to the task-system CLI's 14 verbs and defining the agent-side behaviors (work's Skill lines, status's background summarizer, update's rotation) the scripts deliberately leave to the agent."
  scope:
    covers:
      - "Creating, listing, showing, validating, and summarizing file-backed task folders (tmp/<stub>, dev/tasks/<stub>)"
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
    description: "Single-entry-point CLI at skills/task/scripts/task.py with 14 verb subcommands over task folders, task.yaml records, task_list references, and the global current pointer. 13 verbs are script-driven; status is the one inference verb (the script prints substrate only)."
  layering:
    claude_md: []
    skill_md:
      - "The data model in one breath (folder = SoT, refs inert, id = path, one current pointer, durability = location)"
      - "The canonical venv-python invocation and CLI conventions"
      - "The 14-verb capability surface with per-verb contracts"
      - "The three agent-side behaviors the scripts leave open (work dispatch, status background summary, update rotation)"
    references:
      - "orchestration.md -- the delegation rule (orchestrator does no task work) and the model-routing table for background-agent dispatch"
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
        - "Default dest is tmp (ephemeral). Existing target path errors -- use update, not init. A freeform description argument derives the stub; a path-shaped argument is sanitized, never treated as a path."
        - "After init, the folder holds placeholder scaffolds -- filling them in is agent work per references/handoff-template.md."
    - id: validate
      keywords: [validate task, check task, findings, classification, schema check]
      user_objective: "Check a task against its type schema and structural rules; classify it."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        validate <ref> [--root PATH]
      gotchas:
        - "Contract: prints the classification (active/blocked/closed/archived/invalid/remote) to stdout, errors AND warnings to stderr; exit 0 iff zero findings. All warnings originate here (non-tmp archived folder, uncommitted dev/tasks folder, dangling depends_on/blocked_by, orphaned tmp ref)."
        - "Findings gate work -- errors AND warnings both block. Fix forward (no recovery path), then re-validate."
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
          action: "On success the script writes the current pointer and prints one Skill(skill: \"<name>\") line per skills_to_invoke entry plus an agent_hint: <type> line when set. AGENT BEHAVIOR: actually invoke each emitted skill via the Skill tool now, then dispatch the work to a background agent per references/orchestration.md (delegation rule + model routing), honoring the agent_hint type when set. The script only emits these lines; acting on them is your job."
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
        [--skill-to-invoke NAME ...] [--root PATH] [--pointer PATH]
      gotchas:
        - "Contract: ref defaults to the current task; inits when absent (upsert). Applies the field edits, appends one dated entry to log.md, re-validates, prints the classification; exit 0 iff no findings -- but the write persists regardless (fix-forward). List-valued flags are repeatable and REPLACE the stored list."
        - "AGENT BEHAVIOR: the script's only document write is the dated log.md line -- it never rewrites plan.md. The real rotation discipline (completed-step detail plan -> log, stale-state pass, vocabulary pass, 400-line soft targets) is YOUR work, per references/handoff-template.md, whenever the update represents substantive session progress."
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
        - "Contract: allowed only while the folder still exists (including a tmp archived folder). A task with no folder is gone -- it cannot be reopened. Sets status: active and re-validates (prints classification; exit reflects findings)."
    - id: archive
      keywords: [archive task, finish for good, git is the record, closure policy]
      user_objective: "Retire an active task per its closure policy."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        archive <ref> [--root PATH] [--pointer PATH]
      gotchas:
        - "Contract: acts on an ACTIVE task (a closed task errors with a reopen-first hint). tmp -> status: archived, folder kept. dev/tasks -> folder DELETED; git is the record -- and archive REFUSES an uncommitted (or not-in-git) dev/tasks folder: commit first, no auto-commit. Clears the pointer if current."
    - id: delete
      keywords: [delete task, remove folder, unconditional removal, discard]
      user_objective: "Archive semantics plus unconditional folder removal (even tmp)."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        delete <ref> [--root PATH] [--pointer PATH]
      gotchas:
        - "Contract: same preconditions and uncommitted guard as archive, then the folder is removed even when tmp. Prints deleted: <id>; clears the pointer if current."
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
    - id: show
      keywords: [show task, task details, read fields, inspect]
      user_objective: "Render one task's selected task.yaml fields, cheaply."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        show <ref> [--root PATH]
      gotchas:
        - "Contract: pure field read, no inference. Non-zero with a reason when the ref is unresolvable or the folder is not locally readable (archived / orphaned / remote)."
    - id: status
      keywords: [task status, summarize task, where does it stand, background summary]
      user_objective: "Get a human summary of where a task stands (any task, any state)."
      operation: >-
        ~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/bin/python
        "${CLAUDE_PLUGIN_ROOT}/skills/task/scripts/task.py"
        status <ref> [--root PATH]
      steps:
        - n: 1
          action: "Run the status verb. The script prints the SUBSTRATE only: classification, findings, the task.yaml fields, and the document paths (CLAUDE.md / plan.md / log.md)."
        - n: 2
          action: "AGENT BEHAVIOR: status is the system's ONE inference verb. Dispatch a BACKGROUND sub-agent (Task tool) to read the substrate's documents and produce the summary -- do NOT read plan.md/log.md and summarize inline in the main context. The point is main-context preservation; the sub-agent returns the short summary, you relay it. Summarization is information gathering: route it to sonnet per references/orchestration.md."
      gotchas:
        - "Summarizing inline defeats the verb's reason for existing (context preservation). The script even prints a reminder note to this effect."
  gotchas:
    - "hand-off is the task TYPE name (task.yaml type: field, --type flag), not the skill name -- the skill was renamed hand-off -> task; the v1 type keeps the old name. Do not 'fix' type: hand-off to type: task."
    - "Use the explicit plugin-venv python shown above. The CLI self-repairs (it re-execs under the provisioned venv via its vendored bootstrap_guard), but the venv path is the canonical, cwd-independent invocation."
    - "The current pointer is USER-GLOBAL (one across all projects), matching 'one active task globally'. Tests and sandboxes must inject --pointer; never point real work at a scratch pointer or vice versa."
    - "validate gates work with BOTH errors and warnings -- an uncommitted dev/tasks folder (a warning) blocks work until committed. This is deliberate: durable work that exists only in the working tree is one rm away from gone."
    - "References never carry status. To answer 'is X done?' resolve the folder (show/validate/list) -- do not infer from the presence or wording of a task_list entry."
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
  - id: orchestration
    path: references/orchestration.md
    keywords: [orchestration, delegate, background agent, model routing, fable, sonnet, haiku, opus, dispatch, do no work inline]
    summary: "The delegation rule (the orchestrator does no task work -- every task goes to a background agent) and the model-routing table (fable = deep analysis/complex coding, sonnet = information gathering/simple analysis, haiku = trivial operations, opus = everything else). Load whenever dispatching work to agents."
  - id: handoff_template
    path: references/handoff-template.md
    keywords: [hand-off template, eight sections, CLAUDE.md template, plan rotation, log filter, fill in scaffold]
    summary: "How to fill in and maintain a hand-off-type task folder: the eight-section CLAUDE.md contract, plan.md rotation discipline, log.md filter, anti-patterns, self-verify. Load when populating or updating a scaffolded folder."
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
```
