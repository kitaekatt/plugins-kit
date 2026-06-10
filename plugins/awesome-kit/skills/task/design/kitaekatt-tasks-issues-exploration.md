# kitaekatt-plugins: tasks-kit vs issues-kit — Architectural Exploration

**Repo:** `/Users/christina/Dev/kitaekatt-plugins`
**Plugins:** `plugins/tasks-kit` (v0.1.1) and `plugins/issues-kit` (v0.2.2)
**Date:** 2026-06-09
**Purpose:** Foundation for designing a NEW, simpler task/issue management system derived from these two generations.

---

## Executive Summary

- **issues-kit is the OLDER, more mature generation** (born `issues-plus`, 2026-01-14, shipped "with full test suite"); **tasks-kit is the NEWER, leaner reimagining** built on Claude Code's *native* Task tool. Confidence: **High.**
- The two plugins represent **two fundamentally different storage philosophies**: issues-kit persists issues as **markdown files on disk** (`dev/issues/*.md`, survives across sessions, git-trackable); tasks-kit is a **thin governance wrapper around the ephemeral native Task tool** (in-session task list, no files).
- issues-kit is ~4,630 LOC of Python across 9 scripts + 9 skills + 6 hooks + a second "problems" subsystem; tasks-kit is ~244 LOC, 1 skill, 1 command, 1 hook. The newer one is **~20x smaller**.
- Despite being older, **issues-kit is still the actively maintained workhorse** (last commit 2026-04-22 vs tasks-kit 2026-03-10). tasks-kit is newer in *concept* but did not displace issues-kit.
- The newer tasks-kit's key insight — *"stop reimplementing storage/state/dependencies; delegate them to the native Task tool and only add a thin template/validation layer"* — is the single most important design lesson for a derived system.

---

## PLUGIN 1: tasks-kit (the NEWER generation)

### 1. Purpose & Data Model
Manages **tasks** (not issues). It does **not** define its own storage at all — tasks live in **Claude Code's native Task tool** (TaskCreate/TaskUpdate/TaskGet/TaskList), which is an in-session, ephemeral task list. tasks-kit's entire job is to **gate** access to those tools and **enforce a metadata template** on creation.

- **Storage format:** none of its own — native Task tool (in-memory/session). No markdown files, no YAML issue files.
- **Schema (metadata template, `templates/default.yaml`):**
  - Required: `template` (must be `"default"`), `skills-to-invoke` (array, defaults to `["tools-read"]`).
  - Optional: `priority` (`^P[0-3]$`), `task_type` (enum: implementation/investigation/documentation/orchestration/remediation/review), `agent_hint` (free string), `model` (haiku/sonnet/opus), `labels` (array).
- **Lifecycle/states:** delegated to native tool — `pending → in_progress → completed`. Dependencies via native `addBlockedBy`/`addBlocks`; ownership via native `owner`; background exec via `run_in_background`. The skill explicitly documents *why* it does NOT reimplement these (see SKILL.md "Why Some Metadata Fields Aren't Needed").
- **Signature feature:** **self-documenting tasks** — `skills-to-invoke` is rendered into the task description as explicit `Skill(skill: "...")` instructions via a Handlebars-lite template engine, so any agent picking up the task knows what context to load.

### 2. Architectural Overview
```
plugins/tasks-kit/
  .claude-plugin/
    plugin.json              # name/version/keywords
    plugin-kit.json          # depends-on: [plugins-kit, session-lifecycle-kit]; libs: [hook-logging, skill-tracking, cchooks]
  commands/task.md           # /task slash command (SEQUENCE-mode YAML)
  hooks/
    hooks.json               # registers the PreToolUse gate
    pretooluse/blocking/task-tool-gating.py   # THE enforcement hook
  python/tasks_plus/
    task_template_validator.py  # template load + metadata validation + description embedding
  skills/tasks-write/SKILL.md  # the single skill (gate + template docs)
  templates/default.yaml       # the one template
  _vendor/                     # vendored cchooks + skill_tracking shared libs
```
Flow: user runs `/task` (or calls a Task tool) → PreToolUse hook `task-tool-gating.py` fires → checks (via vendored `skill_tracking.is_skill_invoked`) whether `tasks-write` skill was invoked → if not, **denies** with an instructional message → once invoked, allows; for `TaskCreate` it additionally validates `metadata.template` and the field regexes/enums inline, **fail-closed** (exit 1 / deny on any error).

### 3. Dependencies
- **Python:** none declared (no pyproject.toml, no bootstrap.json) — `task_template_validator.py` imports only `yaml` + stdlib. Hook imports vendored `cchooks` + `skill_tracking` from `_vendor/`.
- **Inter-plugin (`plugin-kit.json`):** `depends-on: [plugins-kit@kitaekatt-plugins, session-lifecycle-kit@kitaekatt-plugins]`; shared `libs: [hook-logging, skill-tracking, cchooks]`.
- **System tools:** none.
- **Bootstrap engine:** relies on the kitaekatt vendor/lib system to populate `_vendor/`, but ships **no `bootstrap.json`** of its own.

### 4. Key Files & Scripting Architecture
- `hooks/pretooluse/blocking/task-tool-gating.py` (the heart): gates `{TaskCreate, TaskUpdate, TaskGet, TaskList}`. Walks up to `hooks/` to find plugin root, injects `_vendor/` + `python/` onto `sys.path`, uses cchooks `create_context()`. Duplicates the template field validation inline (priority regex, task_type/model enums) rather than calling the validator module — a redundancy.
- `python/tasks_plus/task_template_validator.py`: `load_template`, `validate_metadata` (returns `(is_valid, errors, processed)` with defaults applied), `generate_embedded_description` (the `{{#each value}}`/`{{this}}` renderer that turns `skills-to-invoke` into description text).
- `commands/task.md`: a **SEQUENCE-mode** command (verbs `list`/`get`/`complete`/`start`/`help`/`default`). Each verb is a literal step list that invokes `tasks-write` then calls native Task tools. `start <id>` extracts `Skill(...)` patterns from the description and invokes them; multi-id `start` delegates each task to its `agent_hint` subagent in the background.
- **CRUD path:** create = plan-mode → `TaskCreate` (gated+validated); read = `/task list`/`get` → native `TaskList`/`TaskGet`; update/complete = `/task complete` → native `TaskUpdate`. **No indexing/query of its own** — native tool is the index.

### 5. Skills & Commands Inventory
- **Skill `tasks-write`** — gates the four Task tools; documents the template system, metadata fields, and which native features replace metadata.
- **Command `/task`** — list/get/complete/start/help over native tasks (SEQUENCE-mode).

---

## PLUGIN 2: issues-kit (the OLDER generation)

### 1. Purpose & Data Model
Manages **two** artifact types: **issues** (the primary system) and **problems** (a secondary "parking lot" registry). It is a **file-backed, cross-session, git-trackable** system.

- **Issues storage:** flat directory of markdown files at `./dev/issues/*.md` with **YAML frontmatter**; archived issues move to `./dev/issues/archived/`; repeatable templates live in `./dev/issues/repeatable/`. Filenames are kebab-case slugs (`{action}-{domain}-{description}.md`) and double as the identifier.
- **Issue schema:** body sections **Title, Objective, Success Criteria (checkboxes), Completion Criteria**; frontmatter/metadata `priority: P0–P3` (default P2/P3), `status`, `agent_hint`, `claimed_by`/`claimed_at`, `dependencies` (depends_on/blocks with type+reason), `estimated_tokens`, `skill_hints`. **Completion is computed from checkbox state** (`completed_criteria/total_criteria`).
- **Issue lifecycle:** a real **state machine** with validation: `ready → in_progress → complete → archived`, plus `blocked` (has `blocked_by`, auto-unblocks) and `deferred` (manual re-enable). Invalid transitions (e.g. `ready → complete`) are explicitly rejected. `IssueStatus` enum: NO_CRITERIA/IN_PROGRESS/COMPLETE/DEFERRED/BLOCKED.
- **Problems subsystem:** `KNOWN-PROBLEMS.yaml` registry — a parking space for discovered-but-not-yet-actionable issues; a problem **graduates** to an issue when it meets 5 graduation criteria. Separate `/problem` command + `problem_manage.py` (CRUD) + `problem_catalog.py` (per-session `/tmp/session-problems.json` consolidation).

### 2. Architectural Overview
```
plugins/issues-kit/
  .claude-plugin/{plugin.json, plugin-kit.json}   # depends-on: [plugins-kit]; libs: [hook-logging, cchooks]
  bootstrap.json                                  # venv check_imports: [yaml, ruamel]; needs plugins-kit:bootstrap>=0.9.2
  pyproject.toml                                  # name still "issues-plus"; deps pyyaml, ruamel.yaml
  python/
    issue_manage.py       # list/claim/unclaim/archive/check/show  (CLI dispatcher)
    issue_utils.py        # IssueManager + dataclasses (IssueInfo, IssueClaim, DependencyInfo) — the DATA MODEL (1102 LOC)
    issue_create.py       # create from template; regex-infers agent_hint from description
    issue_validation.py   # structure/field validation
    detect_orphaned_issues.py   # find stale claimed issues
    repeatable_issue_start.py   # instantiate a repeatable template -> dated issue
    session_issue_context.py    # track active issue per session (worktree enforcement)
    problem_manage.py     # KNOWN-PROBLEMS.yaml CRUD (re-execs into plugin venv for ruamel)
    problem_catalog.py    # session problem consolidation
  hooks/
    hooks.json
    display-pending-issues.py            # SessionStart: surface open issues
    pretooluse/blocking/issue-commit-validation.py   # gate git commits on issue metadata
    posttooluse/plan_to_issue_converter.py           # convert approved plans -> issue files
    sessionend/unclaim_issues.py                     # release claims at session end
    sessionend/session_end_problem_summary.py        # summarize problems at end
    shutdown-issue-metadata-check.py                 # Stop hook: metadata sanity
    post-issue-closure-check.sh
  skills/   (9 SKILL.md — see inventory)
  templates/{minimal.md, repeatable.md}
  tests/    # real test suite (issues-plus shipped with one)
```
Flow: 5 hooks weave issues into the **whole session lifecycle** — SessionStart surfaces open issues, PostToolUse auto-converts approved plans into issue files, PreToolUse gates commits, SessionEnd unclaims + summarizes, Stop checks metadata. Operations run through `issue_manage.py`, which builds an `IssueManager` (from `issue_utils.py`) rooted at the detected project root (walks up for `CLAUDE.md`/`.git`).

### 3. Dependencies
- **Python:** `pyyaml>=6.0`, `ruamel.yaml>=0.18` (declared in `pyproject.toml`, name still `issues-plus`); dev `pytest`. `bootstrap.json` `venv.check_imports: [yaml, ruamel]`.
- **Inter-plugin:** `depends-on: [plugins-kit@kitaekatt-plugins]`; `libs: [hook-logging, cchooks]`; `bootstrap.json` requires `plugins-kit:bootstrap >= 0.9.2`.
- **Bootstrap engine:** **yes** — depends on the bootstrap engine to provision a per-plugin venv at `~/.claude/plugins/data/plugins-kit/issues-kit/.venv/`. `problem_manage.py` **re-execs itself into that venv** (`os.execv`) so ruamel is importable from any cwd.
- **System tools:** git (project-root detection, commit gating).

### 4. Key Files & Scripting Architecture
- `issue_utils.py` (the model, 1102 LOC): `IssueManager` (find/list/claim/unclaim/archive/parse), dataclasses `IssueInfo`/`IssueClaim`/`DependencyInfo`/`Dependency`, `IssueStatus` enum, checkbox-counting completion logic, dependency parsing, `format_issue_list`.
- `issue_manage.py` (dispatcher): argparse over `list|claim|unclaim|archive|check|show`; list filters `--ready/--mine/--priority/--blocked/--deferred`; `check` parses the file, and if status==COMPLETE **auto-archives** (exit 0 archived / 2 incomplete); `show` prints content + a computed **dependency tree** (forward, blocks, and reverse deps).
- `issue_create.py`: parses description → title/type, **regex-infers `agent_hint`** (e.g. "implement…api" → backend-developer, "document…" → documentation-specialist, "optimize…" → performance-optimizer), writes from `templates/minimal.md` or `repeatable.md`.
- `repeatable_issue_start.py`: copies a repeatable template to `dev/issues/<name>-<YYYYMMDD>.md`.
- `problem_manage.py` / `problem_catalog.py`: the parallel problems registry (graduation scoring, session consolidation).
- **CRUD path:** create = `issue_create.py <template> "<desc>"` → file on disk; read = `issue_manage.py list/show`; update = edit the markdown (gated by `issue-read`/`issue-write` skills); complete = mark checkboxes `[x]` then `issue_manage.py check` → auto-archive. **Index/query = the filesystem** (glob `dev/issues/*.md`) + in-script filters.

### 5. Skills & Commands Inventory (9 skills; commands are skill-with-argument-hint, no commands/ dir)
- **issue** — `/issue` front-door: create/list/show/ready/priority/mine/blocked/unclaim/complete/archive/start-repeatable/orphaned (resolves PLUGIN_ROOT, calls the python scripts).
- **issue-read** — access gate for reading/writing issue files; embeds the lifecycle state machine, file-structure, and update-pattern references (conditional loading).
- **issue-write** — gate write access to issues-plus plugin files.
- **issue-script-write** — autonomous (non-SEQUENCE) access to `issue_manage.py`.
- **issue-management** — higher-level lifecycle orchestration (creation→updates→archival).
- **issue-naming-framework** — naming conventions + priority/orchestration intent inference.
- **problem** — `/problem` registry CRUD over `KNOWN-PROBLEMS.yaml` (list/summary/show/add/update/resolve/delete/validate).
- **persistent-problem-solving** — systematic problem-solving methodology (mental patterns, attack framework).
- **sub-issue-delegation** — delegate non-essential expensive work to sub-agents (README marked ARCHIVED).

---

## CROSS-CUTTING ANALYSIS

### 6. Old vs New Determination — **issues-kit is OLDER, tasks-kit is NEWER.** Confidence: **High.**
Evidence:
- **Git birth dates (pre-rename `-plus` era):** `issues-plus` was added **2026-01-14** in commit `c59aa95` "feat: Add issues-plus plugin with **full test suite**", and an archived issue `extract-issues-plus-plugin.md` documents its extraction the same day. `tasks-plus` has **no footprint before the 2026-01-23 restructure** (`330d5de`); every history probe for `task_template_validator`, the tasks-write skill, and any `tasks-plus/**` file bottoms out at `330d5de`. So issues predates tasks by ~9+ days and arrived already-tested and already-extracted-from-a-larger-system.
- **Maturity gap:** issues-kit = 4,630 LOC / 9 scripts / 9 skills / 6 hooks / a whole second (problems) subsystem / a tests/ dir; tasks-kit = 244 LOC / 1 skill / 1 command / 1 hook / no tests. The newer plugin is dramatically leaner — consistent with a **deliberate rewrite** that offloads work to the native Task tool (which did not exist or was not leveraged when issues-kit was built).
- **Version numbers:** issues-kit 0.2.2 vs tasks-kit 0.1.1 — issues-kit has iterated further (consistent with longer life), tasks-kit is early in its own line.
- **Design language:** tasks-kit's SKILL.md is built **around** the native Task tool and spends a whole section explaining *why it deliberately does NOT reimplement* status/dependencies/ownership/execution-mode — language that only makes sense as a *second-generation* response to a first generation (issues-kit) that DID hand-roll all of those (state machine, claims, dependency trees, archival).

**Nuance worth flagging for the redesign:** "newer" ≠ "successor that won." issues-kit is still the **actively maintained** system (last touched 2026-04-22; tasks-kit last 2026-03-10) and carries the richer feature set. tasks-kit reads as a newer, cleaner *re-conception of the core idea* that did not (yet) replace the older workhorse. They also do not reference each other (no cross-imports) — parallel, not layered.

### 7. Comparison (what the newer tasks-kit changed)
| Dimension | issues-kit (old) | tasks-kit (new) |
|---|---|---|
| **Storage** | Markdown files on disk, git-trackable, cross-session | Native Task tool, in-session, ephemeral |
| **State machine** | Hand-rolled, validated (ready/in_progress/blocked/deferred/complete/archived) | Delegated to native (pending/in_progress/completed) |
| **Dependencies** | Custom parsing + reverse-dep computation + dependency tree | Native `addBlockedBy`/`addBlocks` |
| **Claiming/ownership** | Custom claim files + stale-claim detection + session context | Native `owner` |
| **Completion** | Checkbox counting in the file; auto-archive | Native `status=completed` |
| **Validation** | Issue structure/fields + lifecycle transitions | Metadata template (regex/enum) on create |
| **Agent routing** | Regex-inferred `agent_hint` from description | Explicit `agent_hint` metadata field |
| **Lifecycle integration** | 6 hooks across SessionStart/PostToolUse/PreToolUse/SessionEnd/Stop | 1 PreToolUse gate |
| **Secondary system** | Problems registry (KNOWN-PROBLEMS.yaml) + graduation | none |
| **Self-documenting** | skill_hints field (passive) | `skills-to-invoke` rendered INTO the task description (active) |
| **LOC / surface** | ~4,630 / 9 skills | ~244 / 1 skill |

**Added by new:** native-tool delegation; the embedded-skills self-documenting-task pattern; a clean template/validation layer. **Removed/simplified by new:** on-disk persistence, the full state machine, claiming, dependency-tree computation, the problems subsystem, regex agent inference, and 5 of 6 hooks.

### 8. Design Observations for a Derived "Simpler and More Elegant" System
**Keep (elegant, worth carrying forward):**
- **tasks-kit's core thesis:** do not reimplement storage/state/dependencies/ownership — let the native Task tool own them; add only a thin validation+template layer. This is the single biggest simplification and should anchor the new design.
- **The self-documenting-task pattern** (`skills-to-invoke` → embedded `Skill(...)` instructions in the description). Elegant, makes tasks portable to any agent, low cost. Keep.
- **issues-kit's Success-Criteria-as-checkboxes + auto-complete-when-all-checked.** Completion derived from observable state rather than a manual status flip is genuinely good. Keep the *idea* (criteria-driven completion) even if the storage changes.
- **issues-kit's Objective + Success Criteria + Completion Criteria triad** (WHAT/WHEN-done/return-path, anti-scope-creep). Strong issue hygiene; keep as an optional template, not enforced machinery.
- **Filenames-as-identifiers** (kebab slug) for any file-backed artifacts — simple, greppable, git-diff-friendly.

**Drop / simplify (over-complex):**
- issues-kit's **hand-rolled state machine + claiming + session context + worktree enforcement + reverse-dependency computation** — ~4k LOC of mechanism the native Task tool now subsumes. Most of this is the over-engineering the new generation was reacting against.
- The **6-hook lifecycle weave** — powerful but heavy and hard to reason about; a derived system should pick at most 1–2 (e.g. surface-open-tasks at SessionStart) and make the rest opt-in.
- The **problems/KNOWN-PROBLEMS graduation subsystem** — a whole parallel CRUD app + session catalog. Valuable concept (a parking lot for not-yet-actionable items) but its own product; keep it out of the core, or model it as just a `status: parked` issue rather than a separate registry/script/skill.
- **Skill sprawl (9 skills) and gate redundancy** — issues-kit splits read/write/script-write/management/naming into separate gating skills; tasks-kit duplicates template validation in *both* the hook and the validator module. A derived system wants **one** skill and **one** validation path.
- **Two divergent metadata vocabularies** (issues' frontmatter vs tasks' template) — unify on one schema.

**The essential core of each (what a redesign must preserve):**
- *issues-kit essence:* a **durable, criteria-driven, git-trackable record** of work with a clear definition-of-done.
- *tasks-kit essence:* a **thin, native-delegating governance layer** that enforces a minimal metadata template and makes tasks self-describing.
- **The derived system = tasks-kit's delegation+template spine carrying issues-kit's criteria-driven completion and objective/done hygiene — optionally persistable to a markdown file when durability across sessions is needed, but never reimplementing state/deps/ownership.**

---

## Open Questions / Unknowns
- Does the new system need **cross-session durability** (issues-kit's killer feature the native Task tool lacks)? If yes, a minimal file-backed layer returns — the design choice is how thin it can be.
- Is the **problems/parking-lot** concept in scope, or can it collapse into a task status?
- Should **agent routing** be explicit (tasks-kit) or inferred (issues-kit regex)? Explicit is simpler and more predictable.
- The kitaekatt **vendor/`_vendor`/`plugin-kit.json` lib system** differs from plugins-kit's bootstrap model — a derived system in *this* repo would use plugins-kit bootstrap + `bootstrap.json` instead.
