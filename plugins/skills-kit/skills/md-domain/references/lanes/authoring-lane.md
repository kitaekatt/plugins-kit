# The authoring lane

The ONE authoring procedure, parameterized by artifact. It generalizes the folded
claude-md-authoring technique to all four artifacts and reads the SAME standards
docs the audit lane reads -- in the PRODUCING direction (make it compliant)
rather than the DETECTING direction (find the violation).

**In framework terms this lane is GENERATION.** Generation is the verb: creating
a document that does not exist. When the artifact already exists it is
REGENERATION -- the same verb, same procedure, same standards. EXISTENCE DECIDES
and nothing else does: there is no separate judgment to make, no flag to pass and
no label to choose per run. The preservation half of regeneration is already
carried by the summarize-and-reference rule and its loss-free-deletion guard in
Step 4, which is what keeps a regeneration from spending existing value.

**Single-invocation.** The authoring verb has no fan-out machinery and gains
none: no Workflow lanes, no pre-images, no detect/remediate split, no review
mode. Authoring N files is N runs of this procedure. New authoring machinery
would be new scope.

Load this together with exactly one standards doc, selected by the dispatch
table:

| Lane id | Standards doc | Validation |
|---|---|---|
| `author_skill` | `../standards/skill-standards.md` | `python -m skills_kit_lib.audit <path>` |
| `author_claude_md` | `../standards/claude-md-standards.md` | `python -m skills_kit_lib.audit <path>` |
| `author_project_doc` | `../standards/project-doc-standards.md` | no mechanical validator -- self-check against the standards doc |
| `author_references` | -- | no lane; see below |

**There is no `author_references` lane.** Cross-references are an emergent
property of the other three artifacts, not an authored artifact. A request to
"fix my broken references" is remediation on the `audit_references` lane; a
request to "add a reference" is authoring whichever artifact carries it. Route
there rather than improvising.

## Procedure: author or refine an md artifact (generate, or regenerate when it exists)

**Goal.** Produce an artifact that is schema-valid where a schema exists,
compliant with its standards doc, correctly placed, and shaped in the file's
native form.

**Precondition.** A fact, convention, insight, contract, or document body needs a
home, and the artifact type has been named (or can be resolved in step 1).

### Step 1 -- Confirm the artifact

Confirm which of the four artifacts this is, and that the content belongs there.
The four boundaries:

- **skill** -- a SKILL.md: a contract for a capability the agent invokes. If the
  content is really project-convention knowledge, it belongs in a co-located
  CLAUDE.md; if it is reference body text, it belongs in a skill's `references/`.
- **claude-md** -- a CLAUDE.md carrying a `claude_md:` block (scope, insights,
  optional conventions / glossary). **BRANCH:** if the target sits INSIDE a
  directory of code / YAML / CSV -- a per-directory review-notes file rather than
  a project-root or docs CLAUDE.md -- it is a **code-directory CLAUDE.md**. It
  carries review intelligence, NOT a `claude_md:` schema block. Follow the
  code-directory section of `../standards/claude-md-standards.md` (the shapes,
  the high-value observation kinds, the anchoring and path discipline, the value
  gate); steps 3-5 below apply only in their code-directory form, and the schema
  validator is NOT run on it.
- **project-doc** -- a standalone project document outside the skill tree and
  outside the CLAUDE.md hierarchy (a design record, a reference doc under
  `docs/` or `.claude/docs/`, a README).
- **references** -- not authored; see above.

Before authoring a NEW skill, apply the two-step **skill_packaging_razor** from
`../cohesion-principles.md`: step 1, does the content EXECUTE a process or pull
dynamic information, or is it static reference text (static text is never a
skill)? Step 2, fold into an existing domain if BOTH the vocabulary-reachability
test and the CCP cadence test pass. Do not default a technique to standalone.

Expected: a confirmed artifact type -- or a handoff to the lane that owns the
content.

### Step 2 -- Resolve placement

Read `../cohesion-principles.md` and apply the placement algorithm (CCP change
cadence -> CRP reader set -> ADP load order -> frequency) plus
`placement_follows_trigger_shape` (a TASK-shaped trigger, a verb the session
performs, points at a skill; a LOCATION-shaped trigger, knowledge scoped to a
directory, points at that directory's CLAUDE.md and never graduates to a skill).
Do not re-derive placement from memory.

Per artifact this answers:

- **skill** -- which skill owns this, and does it belong in the SKILL.md body or
  in a `references/` doc (the CRP L2 -> L3 test).
- **claude-md** -- WHICH CLAUDE.md in the load graph (root / subsystem /
  directory / `.local`).
- **project-doc** -- whether a project doc is the right home at all, or the
  content should graduate to a skill, fold into a directory CLAUDE.md, or move
  into an existing skill's `references/`. Project references are the escape-hatch
  and nursery for still-emerging content, not a permanent home. When a project
  doc IS correct, this step also fixes where it sits and which CLAUDE.md or
  SKILL.md will cite it, so it is not born an orphan.

**PRE-RESOLVED PLACEMENT.** When a placement decision arrives already resolved --
an audit remediation naming the destination, an orchestrator directive -- follow
it. Do not re-invoke the placement framework for it; invoke it only for content
without a resolved placement.

Expected: one target file, justified by the placement algorithm (or accepted as
pre-resolved).

### Step 3 -- Apply the artifact's standards doc in the producing direction

Read the bound standards doc and write to it. Each rule is stated as
Rule / Why / Test / Severity; the authoring direction is "satisfy the Rule",
the audit direction is "run the Test". Same document, one SSOT, no second
hand-maintained copy.

- **skill** -- pick the type, then satisfy that type's contract: the required
  floor blocks, the conditionally-required blocks whose criteria fire, and none
  of the prohibited ones. Frontmatter with an activation trigger; a description
  under 160 characters in directive form with an exclusion clause; the YAML
  contract block under the type's root key. Schemas are floors -- add
  load-bearing structured keys beyond the floor freely, but do not declare a
  "recommended" pattern (only required, conditionally required, prohibited).
- **claude-md** -- write or extend the `claude_md:` block: `scope.covers` plus
  `scope.excludes` (the exclusion clause is load-bearing and required), then
  records as `insights` and/or `conventions`. An insight record carries
  `id` / `keywords` (>= 3) / `summary` / `detail` / `origin` / `added`; a
  convention record carries `rule` / `keywords` / `why`. The floor is >= 1 record
  across the insights + conventions UNION, so a conventions-only CLAUDE.md is
  valid; an empty block is not. For a **code-directory** file, follow the
  code-directory standards instead: one of the documented shapes, high-value
  observation kinds only, symbol anchors in preference to line numbers, no
  machine-specific absolute paths, and the value gate applied to every entry.
- **project-doc** -- satisfy the PD rules directly: a single reading task (CRP);
  forward-only load-graph edges; cross-references one hop deep, never chained; no
  back-references into CLAUDE.md sections by name (an orientation mention is
  fine, a content dependency is not); pointers to skill-owned content rather than
  restatement (the owning skill's `references/` stays SSOT); and an inbound
  citation from the CLAUDE.md or SKILL.md that should load it, so it is
  discoverable. For a **README**, the named role applies: the agent-facing copy
  (CLAUDE.md / skill graph) is the SSOT and the README is the derived human
  brief -- never strand a command, convention, or schema there that is not also
  reachable through the agent load graph. For a **generated artifact**, the
  single applicable rule is provenance: name the generator or the generation
  record.

Expected: a body satisfying every rule in the bound standards doc.

### Step 4 -- Shape the content

Apply `../authoring-patterns/content-authoring.md` -- the three content-form
surfaces (YAML frontmatter / markdown text / embedded YAML) and the framework for
choosing between them. Default to structured YAML for LLM-facing content; use
prose only when the content is naturally narrative (an identity sentence, an
orientation paragraph) or when hierarchy carries no meaning. Structure asserts
what prose cannot: a list of records asserts every entry is genuinely of that
kind.

Deeper shape references in the same cluster, loaded on demand:
`three-surfaces.md`, `area-ownership.md`, `area-config.md`,
`actions-pattern.md`, `query-tool-pattern.md`.

Match the surrounding file's existing format and SSOT: extend an existing record
rather than duplicating one. Summarize-and-reference -- a fact lives in one place
and is referenced from elsewhere; an inline reminder is acceptable only when the
referenced fact fits about a dozen tokens, and the loss-free-deletion guard
applies before collapsing any duplicate.

Expected: records in the file's native shape, no duplication.

### Step 5 -- Validate

- **skill and claude-md** -- run the mechanical validator via the plugin venv:

  ```
  (cd ${CLAUDE_PLUGIN_ROOT} && <venvPython> -m skills_kit_lib.audit <path>)
  ```

  Resolve every FAIL (missing `scope.excludes`, keywords under 3, a missing
  required block, a forbidden key indicating mixed-type drift) before considering
  the work done. Zero FAILs is well-formed. Do NOT run it on a code-directory
  CLAUDE.md -- it carries no `claude_md:` block by design.
- **project-doc** -- no mechanical validator exists. Self-check against the
  standards doc's Test column: does every outbound file path resolve, is there at
  least one inbound citation, is every cross-reference one hop, does the body
  serve one reading task.
- **All artifacts** -- when the work was substantial, the honest verification is
  the audit lane on the same artifact (`/md-domain audit <artifact> <path>`).
  Authoring and auditing read the same standards doc, so a clean audit is the
  proof the authoring landed.

Expected: 0 FAILs on the target, or a stated reason a JUDGMENT row is accepted.

## Gotchas

- Re-deriving placement from memory instead of reading the placement spine.
  Which file a fact belongs in is a framework decision, not a guess. (Exception:
  a placement already resolved upstream is followed, not re-derived.)
- Omitting `scope.excludes` on a `claude_md:` block. The exclusion clause is what
  stops adjacent areas from drifting into the file's ownership; the schema
  requires it.
- Keyword clusters under 3 entries on a record. The schema floor is >= 3, for
  chat-term routing.
- A root CLAUDE.md without a `claude_md:` block. Root files SHOULD carry one; add
  it when authoring touches a root file that lacks it. (The audit side treats
  absence on a pre-existing root file as INFO -- adding the block is the
  authoring path's job.)
- Treating a code-directory review-notes file like a schema-block CLAUDE.md, or
  vice-versa. Review-notes files carry gotchas / review checks / boundary claims,
  not a `claude_md:` block, and the schema validator is never run on them.
- Line-only anchors in a code-directory file. Line numbers rot fast; prefer a
  symbol anchor and drop the number unless the gotcha is sub-function.
- Authoring a project doc with no inbound citation. A doc nothing points at never
  loads. Add the pointer from the owning CLAUDE.md or SKILL.md in the same pass.
- Restating skill-owned content in a project doc. When a skill exists for the
  topic, its `references/` is SSOT and the doc collapses to a pointer.
- Declaring a "recommended" pattern in a SKILL.md. Only required, conditionally
  required, prohibited.
- Shipping a verbatim command in a SKILL.md without running it first. Every
  copy-pasteable command is verified against a real environment before shipping
  (see `../skill-domain/example-verification.md`).

## Anti-patterns

- **Same fact in two CLAUDE.mds.** It seems safer -- putting the fact in both the
  root and the subsystem file guarantees the reader sees it. It is wrong: two
  copies drift independently and CCP/SSOT breaks. The placement algorithm yields
  exactly one home; if sibling scopes also need the fact, bubble it up to the
  common parent -- still one copy.
- **Creating a skill for static reference text.** Step 1 of the packaging razor
  rules it out: if the content does not execute a process or fetch dynamic
  information, it is a reference doc inside an existing home, not a skill.
- **Authoring against remembered standards.** The standards doc is the SSOT and
  it is one read away. Writing from memory reintroduces the hand-maintained
  second copy the fold removed.

## Cross-references

- Where a fact lives (the placement spine, incl. the packaging razor) --
  `../cohesion-principles.md`.
- How a fact is shaped -- `../authoring-patterns/content-authoring.md`.
- Skill-artifact deep references (vocabulary, worked examples, domain layering,
  sub-domain schema, script reference) -- `../skill-domain/`.
- Judging an existing artifact instead of producing one -- `audit-lane.md`.
