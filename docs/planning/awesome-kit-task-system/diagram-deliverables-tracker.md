# Task System — diagram deliverables (session tracker)

Per-diagram todo for the architecture/validation diagram set. One item each; build one at a time.
(Native task-list gate wasn't cooperating this session, so this is a plain checklist — itself an
embedded task list, which is the model under design.)

**Diagram workflow:** draft in `tmp/diagrams/` (gitignored) → on approval, move to
`design/diagrams/` (here, `plugins/awesome-kit/skills/task/design/diagrams/`). Renderer modes
(lifecycle/workflow) are too rigid for the operations-as-nodes style → hand-built **architecture-mode**
HTML; the sequence/dataflow renderers fit their cases and are used there. Verify layout with the
render → headless-Chrome `--screenshot` → view loop before showing for approval.

- [x] **Diagram 1 — Task lifecycle with operations** — APPROVED, `design/diagrams/task-lifecycle.html`. (Renderer-based pure-state version retired as a duplicate.)
- [x] **Diagram 2 — Entity / relationship map** — APPROVED, `design/diagrams/task-entities.html`. Document → task_list → Reference{path,host?} → Folder → task.yaml (SoT); Task Type; depends_on/blocked_by; location; cardinalities + invariants.
- [x] **Diagram 3 — `work` operation sequence** — APPROVED, `design/diagrams/task-work-sequence.html` (sequence renderer; terse labels). resolve → validate gate → auto-init → load skills/dispatch.
- [x] **Diagram 4 — Discovery / scoped-list dataflow** — APPROVED, `design/diagrams/task-discovery.html` (dataflow renderer; per-node width to contain text). scope → crawl task.yaml + scan refs → resolve → validate-classify → dedupe-by-path → project fields.
- [x] **Diagram 5 — Location & move workflow** — APPROVED, `design/diagrams/task-move.html` (hand-built; two locations + move engine + refs panel + remote/archive callouts). tmp↔dev/tasks promotion + reference rewrite (id=path) + host/remote branch + uncommitted-archive guard.

Legend: `[ ]` pending · `[~]` in progress (drafted) · `[x]` done (approved + moved to design/diagrams/)

**All five diagrams complete and approved.** The validation set covers: lifecycle (states × operations),
entities (cardinalities + invariants), the `work` operation sequence, discovery/scoped-list
dataflow, and the location & move workflow. Next design artifact: the `task.yaml` schema (§11 of the design doc).
