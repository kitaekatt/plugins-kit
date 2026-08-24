# Task System — Design Specification

**Status:** Design reference. This document specifies the intended entities, relationships, and
operations. **Implementation may supersede it** — where code and this document disagree, the code
is authoritative and this document should be updated or retired.

**Date:** 2026-06-09
**Companion artifacts** - all five diagrams approved and consistent with this spec (audited 2026-06-09):
- [`diagrams/task-lifecycle.html`](diagrams/task-lifecycle.html) — lifecycle (states × operations-as-inputs)
- [`diagrams/task-entities.html`](diagrams/task-entities.html) — entity/relationship map (cardinalities + invariants)
- [`diagrams/task-work-sequence.html`](diagrams/task-work-sequence.html) — `work` operation sequence
- [`diagrams/task-discovery.html`](diagrams/task-discovery.html) — discovery / scoped-list dataflow
- [`diagrams/task-move.html`](diagrams/task-move.html) — location & move workflow
- The two prior systems this derives from are recorded in a maintainer-only
  exploration document, kept in the plugins-kit repository rather than shipped
  with this skill; it is development history and resolves against nothing in a
  consumer's install.

**Location.** This design package lives with the skill it evolves:
`plugins/awesome-kit/skills/task/design/`. The task system **evolves the hand-off skill** (renamed
`task` at Step 6) — the task folder *is* the generalized hand-off folder (see §10).

**Diagram workflow.** Diagrams are drafted in `tmp/diagrams/` (gitignored scratch) and **moved to
`design/diagrams/` (here) when approved**. Only approved diagrams are referenced from this document.

**Provenance.** Derived from three sources, simplified: the **hand-off** skill (the task folder =
generalized hand-off folder), **tasks-kit / issues-kit** (the operation vocabulary and the
file-backed vs native split), and **home-domain `issues.md`** (a hand-maintained tracker that will
adopt this format). The governing idea is the **embedded-YAML typed-unit** model from skills-kit:
structured records living inside markdown documents, discoverable by script.

---

## 1. Overview

A **task** is a unit of work that, once started, owns a **folder**. The folder is the single source
of truth for the task. Tasks are **referenced** from any markdown document (a skill, a reference
doc, a CLAUDE.md); a reference is just a pointer — status is always read from the folder's YAML
record. Tasks are worked by explicit reference; multiple agents can work different tasks in the same repository.

Every task is always named explicitly by ref. There is no implicit "current task" because several agents work in the same repository on different tasks concurrently, so any ambient selection would silently make one agent's ref-less command act on another agent's task.

The system never reimplements what it can derive. State, durability, and discovery are functions of
**where the folder is** and **whether it exists**, not of bookkeeping that can drift.

Design posture: **no backward compatibility, no error recovery.** A malformed task is fixed before
work proceeds — there is no migration path or graceful degradation.

---

## 2. Entities

### 2.1 Task Folder

The materialized task. A directory (the generalized hand-off folder) created when a task is
**started**. An unstarted task has no folder.

- **Default type layout** (`hand-off`): `CLAUDE.md` (auto-loaded orientation), `plan.md`
  (intra-task step list), `log.md` (on-demand history), and **`task.yaml`** (the structured record).
- **Identity = the folder path.** See §5.
- **Source of truth.** All structured task state lives in `task.yaml`. See §6.

### 2.2 `task.yaml` — the structured record

The canonical, script-readable/writable record. Lives at the root of the task folder. This is what
`show`, `list`, `update`, and `validate` read and write. Fields below are the **default
(`hand-off`) type**; the field set and vocabularies are **type-defined** (see §2.5).

```yaml
# task.yaml  (default "hand-off" type)
task:
  _schema_version: "1"
  type: hand-off                  # which task type (selects schema, vocab, closure policy)
  title: "Re-terminate the My Office closet cat6a run"
  status: active                  # active | blocked | closed | archived  (type-defined vocabulary)
  priority: P2                    # type-defined scale (default P1..P3, P1 highest)
  description: |                  # freeform; readable + updatable
    Data path on the closet run is dead. Re-terminate and re-test to rated speed.
  depends_on: []                  # list of reference paths this task needs done first
  blocked_by: []                  # list of reference paths currently blocking this task
  agent_hint: backend-developer   # suggested sub-agent for `work`/dispatch
  skills_to_invoke:               # self-documenting: loaded when the task is worked
    - home-domain
```

**Field contract (default `hand-off` type):**

| Field | Type | Required | Rule |
|---|---|---|---|
| `_schema_version` | string | yes | `"1"`. Dispatches the validator. |
| `type` | string | yes | Registered type name. Default/only: `hand-off`. Selects schema + vocab + closure policy. |
| `title` | string | yes | One-line human title. Non-empty. |
| `status` | enum | yes | One of the type's `state_vocabulary` (default: `active` / `blocked` / `closed` / `archived`). |
| `priority` | string | no | Matches the type's priority pattern (default `^P[1-3]$`, P1 highest). |
| `description` | string | no | Freeform multi-line. |
| `depends_on` | list[path] | no | Reference paths that must be `closed`/`archived` before this is workable. |
| `blocked_by` | list[path] | no | Reference paths currently blocking this task (a task with non-empty `blocked_by` reads as `blocked`). |
| `agent_hint` | string | no | Sub-agent type for `work` dispatch (e.g. `backend-developer`). |
| `skills_to_invoke` | list[string] | no | Skills loaded when the task is worked (the self-documenting-task pattern). This is the task's *additional* set: `work` emits `BASELINE_SKILLS` ahead of it, so the always-required skills need not (and should not) be restated here. |

Notes:
- `abstract` / `invalid` / `orphaned` / `remote` / `gone` are **computed** states (§4), never stored.
- **Schemas are floors, not ceilings** — a type may add load-bearing fields beyond this set.

### 2.3 Reference

A pointer to a task folder, embedded in a markdown document. **Carries no task metadata** — to learn
a task's status you resolve the reference and read its `task.yaml`.

```yaml
# shape of a reference
ref:
  path: dev/tasks/reterminate-office-closet   # the folder path = the task id
  host: macbook                                # OPTIONAL; only meaningful for tmp paths
```

| Field | Type | Required | Rule |
|---|---|---|---|
| `path` | string | yes | The folder path = the task id (§5). `dev/tasks/<stub>` is **project-relative**; `tmp/<stub>` is machine-local. |
| `host` | string | no | Short hostname (`hostname -s`). Only meaningful for tmp paths. Non-matching `host` + tmp ⇒ **remote** (§7.3). |

- A reference is **inert text** until resolved: discovery (§8) collects reference paths, then reads
  each folder's `task.yaml` for state.
- Path canonicalization: paths are normalized (resolve `.`/`..`, project-relative form for `dev/tasks`)
  before equality/dedupe comparisons.

### 2.4 `task_list` — embedded reference list (association)

A `task_list:` typed-unit embedded in a document is just a list of references. **Embedding a
reference in a document associates that task with the document** — this is how "tasks in this skill"
or "tasks in this domain" is expressed. The unit carries paths (+ optional host), nothing more.

```yaml
task_list:
  refs:
    - { path: dev/tasks/reterminate-office-closet }
    - { path: dev/tasks/file-ucg-rma }
    - { path: tmp/spike-ipv6-diag, host: macbook }   # remote if not on macbook
```

A task may be referenced from **multiple** documents; `list` dedupes by canonical path (§8).

- `task_list` is a **typed unit** (a top-level YAML key with a registered schema) embedded in a fenced
  YAML block, per the skills-kit embedded-YAML model. Schema: `{ refs: list[ref] }`, `refs` may be empty.
- Discovery (§8) scans documents for `task_list:` blocks and unions their `refs`. A document with no
  `task_list` contributes nothing; the documents *are* the registry.

### 2.5 Task Type

The pluggable **config bundle** that defines what varies between kinds of task. v1 ships one type
(`hand-off`, the default); the architecture allows more.

A type defines four things:
- **scaffolding template** — which files `init` creates and their shape;
- **embedded-YAML schema** — the `task.yaml` contract `validate` checks against;
- **state vocabulary** — the legal `status` values;
- **closure policy** — what `close` / `archive` / `delete` physically do.

**The default `hand-off` type (the only registered type in v1):**

```yaml
# task type: hand-off
scaffolding:                 # init creates these in the folder
  - CLAUDE.md                #   8-section continuation prompt (the hand-off template)
  - plan.md                  #   accomplished + forward-overview (intra-task task list)
  - log.md                   #   on-demand history
  - task.yaml                #   the structured record (§2.2)
schema: task@1               # the task.yaml field contract in §2.2
state_vocabulary: [active, blocked, closed, archived]
priority_pattern: "^P[1-3]$" # P1 highest
closure_policy:
  close:    "status = closed; keep folder"
  archive:  "tmp: status = archived, move folder to tmp/archived-tasks/<stub> | non-tmp: version control is the record -- git repo: commit final state, delete folder, commit removal; no git repo: record final state, keep folder (agent submits via the workspace's VCS, then delete)"
  delete:   "active or archived precondition + git-dirty guard (no auto-commit; no git check outside a git repo), AND delete the folder even when tmp (unconditional removal)"
```

**Type registration.** A type is identified by the `type:` field in `task.yaml`. v1 ships exactly one
type (`hand-off`), resolved by name from a built-in registry. The registry-extension mechanism (how a
consumer declares a new type) is **deferred** — out of scope for v1; the `type` field reserves the
seam. (Removed from "open questions" — v1 is single-type by decision, not by omission.)


### 2.6 `task_items` — the item enumeration (added 2026-07-09)

An **item** (accepted synonym "work item") is the enumerable unit of next work *within* a task.
The `task_items:` typed unit — a fenced YAML block in the folder's `plan.md`, one per task — is the
single home for the task's open items (`id` / `title` / `state` / optional `priority` reusing the
type's pattern / optional `note`). States are the in-flight triage buckets promoted to contract:
`available` / `in-flight` / `blocked-user` / `deferred`; **completion is removal** from the block.
CLAUDE.md's Immediate Priorities references items by id and never restates state. There is no
sub-task entity: an item needing identity/lifecycle outside its plan is promoted to a task. The
`items` verb enumerates the block; `validate` checks it (block findings are errors; a missing block
is the pre-contract warning). Full contract, evidence, and rationale:
[`task-items-design.md`](task-items-design.md).

### 2.7 `durable_outputs` — documents that outlive the task

A task folder is a **working surface**, not a home for documentation the work outlives. Archiving a
`dev/tasks` folder commits the final state, deletes the folder, and commits the removal — so a
load-bearing document living only in the folder becomes a deleted file: undiscoverable, unindexed,
findable only by someone who already knows it existed.

`durable_outputs` is an optional `task.yaml` list of **repo-relative paths in the owning repo**,
set by the repeatable `update --durable-output PATH` (REPLACES the stored list, like `depends_on` /
`skills_to_invoke`). It declares which documents this task produced that outlive it. **Undeclared
means task-local by declaration.**

The structural point is that the judgment and the check sit at **different times**. The declaration
is made at authoring time, when the author still knows the answer; `archive` only confirms
mechanically that a declaration already made still holds. That separation is what lets archive **ask
the user nothing** — verification is existence + containment + outside-the-folder, never an
assessment of what a document is. An absent field yields a note, never a refusal, so folders
predating the field stay archivable.

**The one-breath authoring test, the placement rule, and the deliberate non-goals are owned by**
[`../references/handoff-template.md`](../references/handoff-template.md), "Durable outputs" — the
SSOT for authors. This section defines only the field and its place in the model.

---

## 3. Relationships

```
Document  --embeds-->  task_list  --contains-->  Reference  --resolves-to-->  Task Folder
                                                      |                              |
                                                 {path, host?}                  task.yaml  (SoT)
                                                                                     |
                                                                              declares  type
                                                                                     |
                                                                              Task Type  --defines-->  schema, vocab, closure, scaffold

depends_on / blocked_by  ----(reference paths)---->  other Task Folders
```

Key invariants:
- **The folder is the source of truth.** References and `task_list` entries hold no status — they
  point; the folder's `task.yaml` answers.
- **Id = path.** Therefore `move` (which changes the path) must rewrite every reference (§7.2).
- **No folder ⇒ not a live task.** What that means depends on the path (§4).
- **Project list is computed**, never stored (§8).

---

## 4. States

There is **no `ready` state.** `init` cannot produce an invalid task, so the output of `init` is an
**`active`** task (valid + extant). `validate` (re-run on every `update`) is the classifier that
decides whether a task is `active`, `invalid`, or `remote`. The lifecycle diagram
([`diagrams/task-lifecycle.html`](diagrams/task-lifecycle.html)) is the canonical picture.

> **Naming note.** Lifecycle `active` means a valid, extant task; task selection is explicit through the task ref and is not a lifecycle state.

| State | Origin | Meaning |
|---|---|---|
| `abstract` | computed | Referenced but **not initialized** — no folder yet. `init`/`work` materializes it. |
| `active` | stored | A **valid, initialized** task. The output of `init`, and of `update` when validation passes. The resting/live state. |
| `invalid` | computed (validate) | Fails validation. Must be **fixed forward** — no back-compat, no recovery — then re-validated. |
| `blocked` | stored | A valid task with unmet `depends_on` / `blocked_by`. Clears back to `active`. *(In the spec; omitted from the lifecycle diagram for clarity.)* |
| `closed` | stored | Work done; folder retained (not yet archived). |
| `archived` | stored / computed | Terminal. tmp: folder marked + **parked** at `tmp/archived-tasks/<stub>` (user-purgeable; a parked folder also reads as `archived` via the tri-state below). non-tmp: final state submitted to version control, folder **deleted** (version control is the record; git is automated, other VCS agent-driven) — which also reads as `archived` via the tri-state below. |
| `orphaned` | computed | A **tmp** reference (local host) whose folder is absent — cleaned up without a proper archive. A defect. *(In the spec; omitted from the lifecycle diagram.)* |
| `remote` | computed (validate) | A **tmp** reference tagged with a non-matching `host`. Assumed to exist there; not locally resolvable. |
| `gone` | computed | No folder **and** no reference anywhere — vanished completely (no tombstone). |

**`validate` classifies into three outcomes:** `active` (valid local), `invalid` (fails), or `remote`
(tmp + host mismatch).

**The "no local folder" resolution (tri-state):**

| Reference | Local folder present? | Resolves to |
|---|---|---|
| non-tmp path | absent | `archived` (expected end state; version control holds the record) |
| tmp path, host = me / unset | absent, parked copy at `tmp/archived-tasks/<stub>` | `archived` (proper archive) |
| tmp path, host = me / unset | absent | `orphaned` (defect) |
| tmp path, host = other | n/a | `remote` (assumed valid on `host`) |

A task with **no folder and no reference anywhere** does not exist — it has vanished completely
(there is no tombstone).

---

## 5. Identity

- **The task id is its folder path.** Unique because paths are unique.
- The **stub** (the folder's base name) is a convenience handle and is **not** guaranteed unique
  (two folders in different locations may share a name). Operations that take a stub must disambiguate
  when more than one folder matches.
- Because the id is the path, **relocating a task changes its id** — which is why `move` rewrites
  references (§7.2).

---

## 6. Source of truth

- **The folder is authoritative.** task.yaml for the task-level record; plan.md's `task_items`
  unit for the item-level enumeration (§2.6). Nothing outside the folder carries task OR item state.
- References and `task_list` entries are **pure associations** — they never duplicate status, so they
  cannot drift from the folder.
- `show` / `list` resolve references to folders and project selected `task.yaml` fields. This is
  **100% script-driven** — the content of YAML fields, no inference.
- `status` (the operation, §7.1) is the exception: it **summarizes** a task and runs in a
  **background agent** to preserve context. It is inference, not a field read.

---

## 7. Operations

The `task` skill exposes one entry point with these verbs. Most are script-driven; `status` is the
inference exception.

### 7.1 Verb catalog

| Verb | Kind | Semantics |
|---|---|---|
| `init` | script | Create the folder + scaffolding for a new task, seeded from current request context. Establishes identity (path), location (§7.4), and type. **Its output is always a valid `active` task — `init` cannot produce an `invalid` one.** |
| `work <ref>` | script | Work the explicitly named task. **Auto-runs `init` if the folder doesn't exist yet** (promotion). Emits one initialization block — the baseline skills merged with the task's `skills_to_invoke`, plus `agent_hint` and the dispatch directive (§7.1). **Gated by `validate`** (§9). |
| `update <ref>` | script | Upsert: `init` if absent, otherwise refresh the folder's state. Appends one dated entry to `log.md` and writes `task.yaml` field edits (`status`, `priority`, `description`, `depends_on`, `blocked_by`, ...). **The script never rewrites `plan.md`; rotation is the agent's hand-off discipline.** **Re-runs `validate`, classifying the task `active` / `invalid` / `remote`** (section 9). |
| `close <ref>` | script | Mark `status: closed`; **keeps** the folder (reopen-able). Acts on an `active` task. |
| `reopen <ref>` | script | Reverse a terminal state back to `active`. **Allowed only if the folder still exists** — incl. a tmp `archived` folder parked at `tmp/archived-tasks/<stub>`, which is **restored** to `tmp/<stub>` first. A task with no folder (and nothing parked) cannot be reopened — it is gone. |
| `archive <ref>` | script | **Operates on an `active` task** (`active -> archived`); to archive a `closed` task, `reopen` it first. **Durable-outputs check first (section 2.7):** every declared path must exist outside the folder, else refuse; absent field -> note, proceed. Per closure policy - **version control is the record** (git is the automated case; no dependency on git): **non-tmp in a git repo** -> commit the final state (status + log entry), delete the folder, commit the removal (two folder-scoped commits); **non-tmp outside git** -> no git command runs; record the final state, keep the folder (`vcs_pending`), agent submits with the workspace's VCS (e.g. `p4 submit`) then runs `delete`; **tmp** -> set `status: archived`, move the folder to `tmp/archived-tasks/<stub>`. |
| `delete <ref>` | script | Operates on an `active` **or `archived`** task (a still-present archived folder — the `vcs_pending` output — is what delete finishes off). **Git-dirty guard** where git can verify (a dirty `dev/tasks` folder refuses; delete never auto-commits; outside a git repo the agent owns VCS state), **and delete the folder even when it is tmp**. Removes the working folder unconditionally. |
| `move <ref> <dest>` | script | Relocate the folder (commonly `tmp/<stub>` → `dev/tasks/<stub>`) **and rewrite every reference** to the new path (§7.2). |
| `status <ref>` | **inference** | Summarize a task — works on **any** task. Resolves the task's classification via `validate`, then **summarizes** in a **background agent** to preserve context. |
| `list [--scope ...]` | script | Enumerate tasks in a scope (§8). Resolves references → folders → projects selected `task.yaml` fields, classifying each via `validate`. Dedupes by path. |
| `show <ref>` | script | Render one task's selected `task.yaml` fields. Cheap, no inference. |
| `items <ref>` | script | Enumerate the task's open items (the plan.md `task_items` unit, §2.6): one line per item — `id  state  priority  title` — sorted by priority then block order; `--state`/`--priority` filter. Ref is required. Cheap, no inference. |
| `validate <ref>` | script | Check the folder/`task.yaml` against the type schema **and the `task_items` unit** (§2.6). Emits errors and warnings. **All warnings originate here.** Gates `work` (§9). |

**Common conventions.** `<ref>` is a path or a stub (stub resolved via §5; ambiguous stub -> error listing candidates). Script verbs exit `0` on success, non-zero on failure/block, and print findings to stderr.

#### Per-verb contracts

- **`init <stub|desc> [--dest tmp|dev/tasks] [--type hand-off]`** — *create.*
  Pre: target path `<dest>/<stub>` (default dest `tmp`) does not already exist (else error — use `update`).
  Steps: scaffold the type's files (§2.5); seed `task.yaml` (`type`, `title`/`description` from context,
  `status: active`); seed `CLAUDE.md`/`plan.md`/`log.md` from the hand-off template; run `validate`.
  Invariant: **output is always a valid `active` task** — if scaffolding can't validate, `init` fails
  (it never leaves an `invalid` task). Writes: the folder. Output: the path.
- **`work <ref>`** — *work explicitly named task.*
  Pre: resolve `<ref>`; if **no folder**, auto-`init` at that path (promotion). Run `validate`; **any
  error OR warning BLOCKS** (exit non-zero, print findings). A **remote** task cannot be worked locally
  (error). Steps: emit **one initialization block** — a header line, then the
  merged skill set (`state_ops.BASELINE_SKILLS`, then the task's `skills_to_invoke`, order-preserving
  and deduped) as `Skill(...)` calls, then `agent_hint` if present, then the closing dispatch
  directive. Writes the folder if auto-init.
  **Why merged script-side:** adherence tracks what the script emits, not what prose requires. Before
  this, `orchestrate` lived only in the skill's prose while the task's own skills were emitted lines —
  producing the predictable partial failure (invoke the declared skills, skip orchestrate, implement
  inline). One emitted list makes the rule "invoke every `Skill(...)` line printed"; the closing
  directive is what converts a loaded `orchestrate` into an actual dispatch.

- **`update <ref> [field edits]`** — *upsert + refresh.*
  Ref is required. Pre: if no folder → `init` (upsert). Steps: apply `task.yaml` field edits
  (`status`/`priority`/`description`/`depends_on`/`blocked_by`); append one dated entry to `log.md`;
  run `validate` -> classify `active`/`invalid`/`remote`. Writes: `task.yaml` and one dated line in
  `log.md`; that dated `log.md` line is the script's only document write. It never rewrites `plan.md`.
  Rotation is the AGENT's discipline under the hand-off template. Output: classification + findings.
- **`close <ref>`** — Pre: folder exists, `status: active`. Set `status: closed`; **keep** folder.
- **`reopen <ref>`** — Pre: folder exists — incl. a tmp `archived` folder parked at
  `tmp/archived-tasks/<stub>`, which is **restored** to `tmp/<stub>` first (**a missing folder with
  nothing parked cannot be reopened** — error). Set `status: active`; re-validate.
- **`archive <ref>`** — Pre: folder exists, `status: active` (to archive a `closed` task, `reopen` first
  — else error). Then: tmp → `status: archived`, **move** the folder to `tmp/archived-tasks/<stub>`
  (occupied parking spot → refuse); non-tmp → **version control is the record**: in a **git repo**,
  write the final state (`status: archived` + dated log entry), **commit** it, **delete** the folder,
  **commit** the removal — two commits pathspec-limited to the task folder, never removing the folder
  before its final state is committed; **outside a git repo**, run **no git command** — write the final
  state, **keep** the folder (`vcs_pending`), and leave submission to the agent/user who knows the
  workspace's VCS (e.g. `p4 submit`), finished by `delete`.
- **`delete <ref>`** — Pre: folder exists, `status: active` **or `archived`** (a still-present archived
  folder is what delete finishes off; `closed` → reopen-first hint). **Git-dirty guard** where git can
  verify (non-tmp folder git sees as **dirty** → refuse — delete never auto-commits; use `archive`);
  outside a git repo no git check applies. Then ensure the folder is removed **even when tmp**
  (unconditional).
- **`move <ref> <dest>`** — Pre: folder exists **locally** (not remote). Steps: relocate folder
  `old → <dest>/<stub>`; scan project-scope documents for the old path; **rewrite every reference** to
  the new path (project-relative when `dest` is `dev/tasks`).
  Writes: folder location and N documents. (§7.2)

- **`status <ref>`** — *(inference)* Resolve + `validate` to classify, then a **background agent**
  summarizes `task.yaml` + `plan.md`/`log.md`. Works on **any** task. The only inference verb.
- **`list [--scope user|project|skill|file <target>] [--status … --priority …]`** — Discovery (§8) →
  resolve → classify each via `validate` → **dedupe by canonical path** → project `id`/`title`/`status`/
  `priority`. Remote tasks are listed as opaque (`@host`, status unresolved). Script-only.
- **`show <ref>`** — Resolve → print selected `task.yaml` fields. Cheap, no inference.
- **`validate <ref>`** — §9. Emit errors + warnings; classify `active`/`invalid`/`remote`. Exit `0` iff
  no findings.

### 7.2 `move` rewrites references

Because **id = path**, moving a folder changes its id. `move` therefore:
1. relocates the folder, then
2. scans every document for references to the old path and rewrites them to the new path.

A move that skipped step 2 would leave **orphaned references** — interpreted as `archived` if they
point at a (now-absent) non-tmp folder, or `orphaned` if they point at an absent tmp folder. Both are
defects `move` exists to prevent.

### 7.3 Remote tasks

A reference with a `host` that does not match the current host, pointing at a **tmp** path, is a
**remote task**. The folder is **assumed to exist** on `host`; it is not verified or fetched.

Local behavior on a remote task:
- `list` shows it as `<path> @host — remote, status not locally resolvable` (its `task.yaml` can't be read).
- `work` / `validate` / `move` / `close` / `update` cannot act on it locally (the folder isn't here).

A remote task is effectively a **read-only existence pointer** locally — "work for this is happening
on `host`." (v1 does not attempt SSH/remote fetch.)

### 7.4 Location and durability

Durability is a **per-task location choice**, not a system-wide mode:

| Location | Tracking | Use |
|---|---|---|
| `tmp/<stub>` | none (ephemeral) | default; session-local work |
| `dev/tasks/<stub>` | version-controlled (git automated; other VCS agent-driven) | durable, auditable work |

`move` promotes/demotes between them and rewrites references. Git-tracked documents may reference tmp
tasks via the optional `host` parameter (§7.3).

**Uncommitted-archive guard (revised 2026-07-22).** `archive` deletes a non-tmp folder on the
assumption version-control history is the record. A `dev/tasks/<stub>` deleted without a submitted
record would be lost. Original resolution: `validate` warns and `archive` refuses until the user
commits. Revision — with the principle that **the task system has no dependency on git** (version
control is the record; git is merely the VCS the scripts can detect and automate):

- **In a git repo**, `archive` **records the final state itself** — it writes `status: archived` + a
  dated log entry, commits, deletes the folder, and commits the removal (both commits pathspec-limited
  to the task folder, so unrelated staged work never rides along), never removing the folder before
  the final-state commit succeeds.
- **Outside a git repo**, the scripts run **no git command** and pass no judgment — the workspace may
  use Perforce or another VCS the agent understands. `archive` records the final state and **keeps**
  the folder (`vcs_pending`); the agent submits it with the workspace's VCS, then `delete` (which
  accepts `status: archived` and applies no git check outside a repo) removes it.
- `delete` keeps the refuse-when-dirty guard **where git can verify it** (it never auto-commits — it
  is the no-ceremony removal verb).
- The **`validate` warning** for a git-dirty `dev/tasks` folder remains (warnings gate `work`, §9);
  outside a git repo it is an advisory **note** ("version-control state unverified"), never a blocking
  warning — a Perforce-backed workspace must not be permanently gated by a git check.

---

## 8. Discovery

Two crawl modes, both script-driven:

1. **By folder (primary).** Discover `task.yaml` files under the known roots (`dev/tasks/`, tmp) — the
   authoritative enumeration of live tasks.
2. **By reference (scoped association).** Read the `task_list` references embedded in a given document
   set to get the tasks *associated with* that scope.

**Scopes** (a query is constrained to where you point it):

| Scope | Set crawled |
|---|---|
| `user` | user-level task roots (`~/.claude/{tmp,dev/tasks}`) |
| `project` | the project's task roots (`<project>/{tmp,dev/tasks}`) — **always computed**, never a stored master list |
| `skill` | one skill's `SKILL.md` **plus its `references/`** |
| `file` | a single document |

**Algorithm (`list --scope <s> [target]`):**
1. **Resolve scope → roots + document set:**
   - `user` → roots `~/.claude/{dev/tasks,tmp}`-equivalent; docs = `*.md` under those roots.
   - `project` → roots `<project>/dev/tasks` + `<project>/tmp`; docs = `*.md` **under those roots only**,
     excluding the parked `tmp/archived-tasks/` subtree (parity with the folder crawl).
   - `skill <name>` → docs = that skill's `SKILL.md` **plus its `references/*.md`**; roots as project.
   - `file <path>` → docs = that one file; roots as project.

   **Why `project`/`user` do not crawl the whole tree.** An embedded `task_list:` block is
   indistinguishable from an *example* of one, so a whole-tree crawl reports the format's own
   documentation as live tasks — §2.4's sample block did exactly that in the repo that develops this
   skill, yielding three phantom tasks with no folders behind them. Association is therefore **explicit
   rather than implicit**: a `task_list` embedded outside the task roots is reached by naming its
   document (`--scope skill <name>` / `--scope file <path>`), not by an ambient scan.
2. **Collect candidate paths:** union of (a) folder crawl — every `task.yaml` under the roots, taken as
   its folder path; and (b) reference scan — every `refs[].path` in a `task_list:` block in the doc set.
3. **Canonicalize + dedupe** by path (§2.3): one entry per task even if folder-found *and* referenced,
   or referenced from many docs.
4. **Classify each** via `validate`: `active`/`blocked`/`closed`/`archived` (read from `task.yaml`), or
   computed `remote` (tmp + host mismatch — opaque, not read) / `orphaned` (tmp ref, local, no folder).
5. **Project + filter:** emit `id`(path) · `title` · `status` · `priority`; apply `--status`/`--priority`
   filters. Archived-with-surviving-folder are included; folderless-non-tmp refs read as `archived`.

**Dedupe:** a task referenced from multiple documents appears **once**, keyed by canonical folder path.
References carry no metadata to merge — the folder's `task.yaml` is the single record.

The home-domain `issues.md` is one ordinary embedding host under this model; there is **no canonical
stored master list**. The project view is the computed `list --scope project` — which enumerates the
task roots. An embedding host that lives outside them (a skill doc, a domain `issues.md`) is queried
by naming it: `list --scope file <path>` or `--scope skill <name>`.

---

## 9. Validation

`validate <ref>` checks a task against its **type schema** and the structural rules, classifies it, and
emits findings. It is a **gate on `work`** — **both errors and warnings block** (you cannot work a task
with any open finding). **All warnings originate here.** No back-compat, no recovery: findings are fixed
forward.

**Classification (the outcome):**
- **`remote`** — tmp path + `host` ≠ current host. Short-circuits: not read or further validated locally.
- **`invalid`** — any **error** below.
- **`active`/`blocked`/`closed`/`archived`** — no errors; `status` read from `task.yaml` (`blocked` when
  `blocked_by` is non-empty).

**Errors (block; task is `invalid`):**

| Condition | Detail |
|---|---|
| missing `task.yaml` | folder exists but has no record |
| unparseable YAML | `task.yaml` not valid YAML |
| schema violation | missing required field, wrong type, `status`/`priority` outside the type vocabulary, unknown `_schema_version` |
| missing scaffolding | a file the type's `scaffolding` requires is absent |
| unknown `type` | `type:` names no registered type |

**Errors from the `task_items` unit (§2.6; block; task is `invalid`):** unparseable/schema-failing
block, state outside the item vocabulary, priority outside the type pattern, non-kebab or duplicate
`id`, more than one block, or a block outside plan.md.

**Warnings (also block `work`; do not by themselves make a task `invalid`):**

| Condition | Detail |
|---|---|
| non-tmp `status: archived` folder | version control is the record — submit any pending state, then `delete` the folder (a `vcs_pending` archive awaiting its finishing `delete` sits in exactly this state) |
| uncommitted `dev/tasks` folder | git sees unsaved durable work; commit it — `archive` commits the final state itself, `delete` refuses until committed. Outside a git repo this is an advisory *note* ("version-control state unverified"), not a warning (§7.4) |
| dangling `depends_on`/`blocked_by` | references a path with no resolvable task |
| orphaned tmp reference | a tmp ref (local host) whose folder is absent (§4) |
| no `task_items` block in plan.md | pre-contract folder; prompts the one-time forward conversion (§2.6) |
| stale item reference in CLAUDE.md | a backticked hyphenated id under Immediate Priorities matching no item |
| oversized document | a top-level `*.md` over its line ceiling (CLAUDE.md/plan.md 400, other docs 800; log.md and `log-*.md` exempt — the history sink rotation targets). The finding names the largest `##` sections; the fix is decomposition per the rotation strategy, not trimming (2026-07-20) |

**Notes (advisory third tier, added 2026-07-20; NOT findings — never gate, never affect exit codes):**
approaching-budget (a doc past its healthy target: CLAUDE.md 250 lines, plan.md 300),
dominant-section (a single `##` section over half of a 150+-line CLAUDE.md/plan.md — the accretion
pattern caught before the ceiling), and session-diary (more than 3 `**YYYY-MM-DD` narrative markers in
CLAUDE.md). Thresholds and remedies: the task skill's `references/handoff-template.md`, "Document size
budgets"; constants at the top of `scripts/task_system/validate.py`.

**Reuse of audit machinery.** `validate` is intended to run the **same typed-unit schema validation**
skills-kit uses for embedded YAML (the `task`/`task_list` units registered as schemas). *Resolved (§10):
the wiring is a **shared library** — `skills_kit_lib` exposed via bootstrap `shared_libs` and imported
by awesome-kit.* The **rules above are the contract** regardless of how validation is wired.

---

## 10. Decisions

**Resolved (2026-06-09):**
- **Packaging / where this is built** → the task system **evolves the `hand-off` skill in awesome-kit**
  (the task folder *is* the generalized hand-off folder). Design lives in
  `plugins/awesome-kit/skills/task/design/`; approved diagrams in `design/diagrams/`.
- **Uncommitted non-tmp archive** (§7.4, revised 2026-07-22) → version control is the record; **no
  dependency on git**. In a git repo `validate` **warns** and `archive` **commits the final state +
  removal itself**; outside a git repo the scripts run no git command — `archive` records the final
  state and keeps the folder (`vcs_pending`) for the agent to submit via the workspace's VCS, and
  `validate` emits an advisory note. `delete` keeps the refuse-when-git-dirty guard (no auto-commit).
- **Type registration** (§2.5) → v1 ships exactly one type (`hand-off`); the `type` field reserves the
  extension seam, but the registry-extension mechanism is **out of scope for v1**.

**Open — small (resolve during implementation):**
- **Stub disambiguation UX**: how `work <stub>` resolves when multiple folders share a base name. Current
  rule: error and list candidates; a sharper UX (prefer current-project, interactive pick) is a polish item.

**Resolved (2026-06-09, post-hand-off Step 0 — user decisions):**
- **Cross-plugin schema coupling** (B) → **shared lib via bootstrap.** skills-kit declares
  `skills_kit_lib` in its `bootstrap.json` `shared_libs`; awesome-kit imports it via
  `shared_lib_imports` (standalone scripts use the vendored `bootstrap_guard.reexec_under_plugin_venv`
  pattern per `plugins/CLAUDE.md`). `validate` calls `skills_kit_lib.schema_engine` directly; the
  `task`/`task_list` schema dicts live in awesome-kit (they change with the task system, CCP).
- **Hand-off evolution shape** (B) → **rename `hand-off` → `task`.** Single clean surface; `/hand-off`
  breaks for consumers at the next publish (accepted). Rename lands with Step 6 (skill wiring).
- **Skill / command surface** (B) → **domain-skill with dispatch only.** One `task` skill routes verb
  requests to per-verb scripts; no slash command.
- **Implementation model** (D) → **sub-agents on Fable 5.** The main session orchestrates and
  integrates; each plan step is delegated to a Fable 5 sub-agent (single-tier).

---

## 11. Status & next

**Section A (spec) is complete** — entities, schemas (§2.2–§2.5), relationships, states, identity, SoT,
per-verb operation contracts (§7), discovery algorithm (§8), and validation rules (§9) are specified and
audited consistent with the five approved diagrams. This document is the **implementation contract**.

**Next (per the agreed plan):** a **hand-off** of this design to a fresh session, then the post-hand-off
steps — the B/C/D decisions above, then phased implementation (schemas + validate → `init`/scaffolding →
read ops → state ops → destructive/location ops → skill wiring), tests throughout. Implementation
dependencies to confirm: `pyyaml`/`ruamel` in awesome-kit's `pyproject.toml` + `bootstrap.json`
`check_imports`; host detection via `hostname -s`.
