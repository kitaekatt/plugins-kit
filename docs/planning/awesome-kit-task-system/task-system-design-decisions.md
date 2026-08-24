# Task System Design -- Decision Record (maintainer-only)

**What this is.** The decision and status record for the awesome-kit task
system: which options were weighed, the dated resolutions, who implemented
what, and the remaining polish items.

**Maintainer-only -- this does not ship.** It is plugins-kit development
history, not guidance for a consumer of the `task` skill. It lives in the
plugins-kit repository and is never copied into a plugin cache.

**Extracted on 2026-08-24** from
`plugins/awesome-kit/skills/task/design/task-system-design.md` (sections 10 and
11 of that specification). The section numbering below is kept as it was in the
source document so existing citations still resolve. The specification itself
continues to ship and remains the implementation contract; the contracts these
decisions produced are stated there.

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

## Front matter removed from the shipped specification (2026-08-24)

Three blocks were trimmed from `plugins/awesome-kit/skills/task/design/task-system-design.md`
in the same pass that extracted sections 10 and 11. Each answered "who reads this on a machine
that is not ours" with "nobody".

**Diagram approval stamp.** The Companion artifacts heading read: "all five diagrams approved and
consistent with this spec (audited 2026-06-09)". An internal approval record, not a fact about the
diagrams a consumer reads.

**Diagram workflow.** "Diagrams are drafted in `tmp/diagrams/` (gitignored scratch) and moved to
`design/diagrams/` (here) when approved. Only approved diagrams are referenced from this document."
A consumer's install has no `tmp/diagrams/`.

**Provenance.** "Derived from three sources, simplified: the hand-off skill (the task folder =
generalized hand-off folder), tasks-kit / issues-kit (the operation vocabulary and the file-backed
vs native split), and home-domain `issues.md` (a hand-maintained tracker that will adopt this
format)." Derivation history naming prior private systems. The one load-bearing sentence it
carried -- the embedded-YAML typed-unit model as the governing idea -- was kept in the shipped
specification under a Governing idea heading.
